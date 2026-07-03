"""PRNG control sources — seeded, reproducible, loudly NOT quantum.

These exist as matched statistical controls for the quantum devices in
experiment arms (never as an entropy substitute). Every control is
constructed from a required 128-bit seed and keeps a monotonically
increasing ``stream_offset_bytes``, so any served block is regenerable
offline from ``(source_id, seed, stream_offset_bytes)`` alone:

- ``prng_uniform`` — the canonical stream is the little-endian byte
  stream of successive PCG64 64-bit raw outputs. Deliberately NOT
  ``numpy.random.Generator.bytes()``: that draws ``ceil(n/4)`` words per
  call and truncates, so concatenation would depend on request chunking
  and ``(seed, offset)`` could not regenerate a block. Raw words +
  ``PCG64.advance`` give an O(1)-seekable, chunking-independent stream
  (see :func:`uniform_stream_bytes`).
- ``prng_markov`` — order-1 byte Markov chain fitted to a quantum
  card's fingerprint (npz model from ``scripts/fit_markov.py``). One
  64-bit raw draw per output byte via inverse-CDF; ``prev_byte``
  persists across requests within a server session, so regeneration
  replays the chain from the stream start
  (see :func:`markov_stream_bytes`).

Layering: imports nothing internal except :mod:`.config`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np
from numpy.random import PCG64

from .config import ConfigError, ControlConfig

logger = logging.getLogger(__name__)

#: Transition/initial rows must sum to 1 within this tolerance.
_ROW_SUM_TOLERANCE = 1e-9


def _warn_not_quantum(config: ControlConfig) -> None:
    """Startup warning mirroring the MockDevice convention in devices.py."""
    logger.warning(
        "Control %s is a PRNG (%s) — pseudorandom, NOT quantum, NOT freshly "
        "measured. Seeded control source for experiment arms only.",
        config.id,
        config.type,
    )


# ── uniform ──────────────────────────────────────────────────────────────


def uniform_stream_bytes(seed: int, offset: int, n: int) -> bytes:
    """Bytes ``[offset, offset+n)`` of the canonical uniform stream for *seed*.

    The stream is the little-endian byte concatenation of successive
    PCG64(seed) 64-bit raw outputs. ``PCG64.advance(k)`` skips exactly
    ``k`` raw draws, so any offset is O(1)-seekable and the result is
    independent of how earlier requests were chunked.
    """
    if n <= 0:
        return b""
    bit_gen = PCG64(seed)
    first_word, skip = divmod(offset, 8)
    if first_word:
        bit_gen.advance(first_word)
    n_words = (skip + n + 7) // 8
    words = bit_gen.random_raw(n_words)
    return words.astype("<u8").tobytes()[skip : skip + n]


class PrngUniformControl:
    """Seeded uniform PRNG source (numpy PCG64). NOT quantum."""

    kind = "prng"

    def __init__(self, config: ControlConfig) -> None:
        _warn_not_quantum(config)
        self.config = config
        self._seed = config.seed_int
        self.stream_offset_bytes = 0

    def read(self, n: int) -> bytes:
        """The next *n* bytes of the stream; advances ``stream_offset_bytes``."""
        data = uniform_stream_bytes(self._seed, self.stream_offset_bytes, n)
        self.stream_offset_bytes += n
        return data


# ── markov ───────────────────────────────────────────────────────────────


def load_markov_model(path: str) -> tuple[np.ndarray, np.ndarray, dict, str]:
    """Load + validate an npz Markov model.

    Returns ``(initial, transition, meta, model_sha256)``. Raises
    :class:`ConfigError` on a missing file or an invalid model — the
    server must never start on a silently broken control.
    """
    model_path = Path(path)
    if not model_path.is_file():
        raise ConfigError(f"prng_markov model file not found: {path}")
    model_bytes = model_path.read_bytes()
    sha256 = hashlib.sha256(model_bytes).hexdigest()
    with np.load(model_path) as npz:
        missing = {"initial", "transition", "meta"} - set(npz.files)
        if missing:
            raise ConfigError(f"prng_markov model {path}: missing array(s) {sorted(missing)}")
        initial = np.asarray(npz["initial"], dtype=np.float64)
        transition = np.asarray(npz["transition"], dtype=np.float64)
        meta_raw = str(npz["meta"])
    if initial.shape != (256,):
        raise ConfigError(f"prng_markov model {path}: `initial` must be float64[256]")
    if transition.shape != (256, 256):
        raise ConfigError(f"prng_markov model {path}: `transition` must be float64[256,256]")
    if abs(float(initial.sum()) - 1.0) > _ROW_SUM_TOLERANCE:
        raise ConfigError(f"prng_markov model {path}: `initial` does not sum to 1")
    row_err = float(np.max(np.abs(transition.sum(axis=1) - 1.0)))
    if row_err > _ROW_SUM_TOLERANCE:
        raise ConfigError(
            f"prng_markov model {path}: `transition` rows must sum to 1 "
            f"(max deviation {row_err:.3e} > {_ROW_SUM_TOLERANCE})"
        )
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"prng_markov model {path}: `meta` is not valid JSON") from exc
    return initial, transition, meta, sha256


def _markov_step_bytes(
    cum_initial: np.ndarray,
    cum_transition: np.ndarray,
    raws: np.ndarray,
    prev: int | None,
) -> tuple[bytes, int | None]:
    """Advance the chain by one byte per 64-bit raw draw (inverse-CDF)."""
    out = bytearray()
    for raw in raws:
        u = float(raw) / 2**64
        row = cum_initial if prev is None else cum_transition[prev]
        byte = int(np.searchsorted(row, u, side="right"))
        if byte > 255:  # float rounding at the top of the CDF
            byte = 255
        out.append(byte)
        prev = byte
    return bytes(out), prev


def markov_stream_bytes(model_path: str, seed: int, offset: int, n: int) -> bytes:
    """Offline regeneration of bytes ``[offset, offset+n)`` of a markov stream.

    The chain state depends on every earlier byte, so regeneration
    replays from the stream start (one raw draw per byte) and returns
    the final *n* bytes. Byte-identical to what a live control with the
    same seed served across any request chunking.
    """
    if n <= 0:
        return b""
    initial, transition, _, _ = load_markov_model(model_path)
    raws = PCG64(seed).random_raw(offset + n)
    data, _ = _markov_step_bytes(
        np.cumsum(initial), np.cumsum(transition, axis=1), raws, None
    )
    return data[offset:]


class PrngMarkovControl:
    """Order-1 byte Markov PRNG fitted to a device fingerprint. NOT quantum."""

    kind = "prng"

    def __init__(self, config: ControlConfig) -> None:
        _warn_not_quantum(config)
        self.config = config
        initial, transition, self.meta, self.model_sha256 = load_markov_model(config.model)
        self._cum_initial = np.cumsum(initial)
        self._cum_transition = np.cumsum(transition, axis=1)
        self._bit_gen = PCG64(config.seed_int)
        self._prev: int | None = None  # persists across requests in a session
        self.stream_offset_bytes = 0

    def read(self, n: int) -> bytes:
        """The next *n* bytes of the chain; advances ``stream_offset_bytes``."""
        if n <= 0:
            return b""
        raws = self._bit_gen.random_raw(n)
        data, self._prev = _markov_step_bytes(
            self._cum_initial, self._cum_transition, raws, self._prev
        )
        self.stream_offset_bytes += n
        return data


def make_control(config: ControlConfig) -> PrngUniformControl | PrngMarkovControl:
    """Construct the control for a validated :class:`ControlConfig`."""
    if config.type == "prng_uniform":
        return PrngUniformControl(config)
    return PrngMarkovControl(config)
