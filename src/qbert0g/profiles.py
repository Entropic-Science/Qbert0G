"""Profile transform library — pure, deterministic functions over bytes.

A profile is a named source defined as ``transform(inputs...)`` over
devices and/or PRNG controls (never other profiles). This module holds
the NORMATIVE transform semantics; reading the inputs, pairing, locks
and provenance belong to the SourceRouter (sources.py, upcoming).

Bit order (NORMATIVE): numpy ``unpackbits`` / ``packbits`` default —
**MSB first within each byte**. Bit 0 of a stream is the most
significant bit of byte 0. Pinned by tests/test_profiles.py.

Transforms:

- ``identity(a)`` — passthrough; exists so a profile id (with its
  provenance logging) can front a plain device.
- ``xnor(a, b)`` — agreement: output bit = 1 where the input bits
  agree; byte i = ``~(a[i] ^ b[i]) & 0xFF``. Consumes n bytes from
  EACH input per n output bytes.
- ``parity(a; taps, stride)`` — over the input bitstream r, output
  bit i = XOR of ``r[stride*i + t]`` for each tap t. Producing M output
  bits consumes ``stride*(M-1) + max(taps) + 1`` input bits.

- ``hash_truncate(a; hash, in_bytes, keep_bytes)`` — request-selected
  conditioning (not a ``profiles:`` transform): the input is cut into
  consecutive ``in_bytes`` blocks, each block is hashed on its own, and
  only the FIRST ``keep_bytes`` of each digest are kept and
  concatenated. At least half of every digest is always discarded
  (``keep_bytes <= digest_size // 2``) and every block holds at least
  twice the bytes it yields (``in_bytes >= 2 * keep_bytes``), so the
  output is always far shorter than the input it was computed from.
  Stateless across blocks and requests: the output is a pure function
  of the raw block, which is what provenance replay re-checks.

Layering: imports nothing internal except :mod:`.config`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .config import (
    CONDITIONING_HASHES,
    CONDITIONING_MODES,
    TRANSFORM_ARITY,
    ConditioningConfig,
    ProfileConfig,
)


def unpack_msb_first(data: bytes) -> np.ndarray:
    """Bytes → bit array in the NORMATIVE order (MSB first within each byte).

    The single unpack seam shared by the transforms and the CLI bitstream
    viewer, so every bit-level view of a stream agrees on bit order.
    """
    return np.unpackbits(np.frombuffer(data, np.uint8))


def identity(a: bytes) -> bytes:
    """Passthrough of one input."""
    return a


def xnor(a: bytes, b: bytes) -> bytes:
    """Agreement of two equal-length streams: output bit = 1 where they agree."""
    if len(a) != len(b):
        raise ValueError(f"xnor inputs must be equal length, got {len(a)} and {len(b)}")
    arr_a = np.frombuffer(a, dtype=np.uint8)
    arr_b = np.frombuffer(b, dtype=np.uint8)
    return np.bitwise_not(arr_a ^ arr_b).tobytes()


def parity_bits_needed(out_bytes: int, taps: Sequence[int], stride: int) -> int:
    """Input bits consumed to produce ``out_bytes`` of parity output."""
    m = out_bytes * 8
    return stride * (m - 1) + max(taps) + 1


def parity_bytes_needed(out_bytes: int, taps: Sequence[int], stride: int) -> int:
    """Input bytes consumed to produce ``out_bytes`` of parity output."""
    return (parity_bits_needed(out_bytes, taps, stride) + 7) // 8


def parity(raw: bytes, out_bytes: int, taps: Sequence[int], stride: int) -> bytes:
    """Tapped XOR decimation of the input bitstream (normative reference).

    Output bit i = XOR of input bits ``stride*i + t`` for each tap t,
    packed MSB-first. *raw* must hold at least
    :func:`parity_bits_needed` bits.
    """
    m = out_bytes * 8
    need = stride * (m - 1) + max(taps) + 1
    if len(raw) * 8 < need:
        raise ValueError(
            f"parity needs {need} input bits ({(need + 7) // 8} bytes) for "
            f"{out_bytes} output bytes, got {len(raw)} bytes"
        )
    bits = unpack_msb_first(raw)[:need]
    idx = stride * np.arange(m)
    out = np.zeros(m, np.uint8)
    for t in taps:
        out ^= bits[idx + t]
    return np.packbits(out).tobytes()


# ── request-selected conditioning ────────────────────────────────────────

#: gRPC request metadata keys selecting the conditioning of ONE call
#: (lower-case, as gRPC delivers them). Absent ``qr-conditioning`` means
#: ``raw`` — the daemon's behaviour is unchanged for callers that send
#: nothing.
CONDITIONING_HEADER = "qr-conditioning"
CONDITIONING_HASH_HEADER = "qr-conditioning-hash"
CONDITIONING_IN_BYTES_HEADER = "qr-conditioning-in-bytes"
CONDITIONING_KEEP_BYTES_HEADER = "qr-conditioning-keep-bytes"
#: Initial-metadata key the daemon answers with: the canonical string of
#: the conditioning it APPLIED. A client that asked for a mode and does
#: not see it echoed must not use the bytes.
CONDITIONING_APPLIED_HEADER = "qr-conditioning-applied"


class ConditioningError(ValueError):
    """An invalid or refused per-request conditioning selection."""


@dataclass(frozen=True)
class Conditioning:
    """The validated conditioning of one request.

    ``raw`` carries no parameters. ``hash_truncate`` carries the hash
    name and the two block sizes; see the module docstring for the
    normative definition.
    """

    mode: str = "raw"
    hash: str = ""
    in_bytes: int = 0
    keep_bytes: int = 0

    @property
    def is_raw(self) -> bool:
        return self.mode == "raw"

    def canonical(self) -> str:
        """The echo / provenance string, e.g. ``hash_truncate;sha256;128;16``."""
        if self.is_raw:
            return "raw"
        return f"{self.mode};{self.hash};{self.in_bytes};{self.keep_bytes}"

    def raw_bytes_needed(self, out_bytes: int) -> int:
        """Raw bytes to read from the source to serve *out_bytes*."""
        if self.is_raw:
            return out_bytes
        return hash_truncate_bytes_needed(out_bytes, self.in_bytes, self.keep_bytes)

    def apply(self, raw: bytes, out_bytes: int) -> bytes:
        """Condition *raw* down to exactly *out_bytes* bytes."""
        if self.is_raw:
            return raw
        return hash_truncate(raw, out_bytes, self.hash, self.in_bytes, self.keep_bytes)

    def provenance(self, raw: bytes) -> dict:
        """The provenance fact: the parameters plus the raw block's sha256.

        Replay: re-apply the transform to the raw block (regenerated for
        a PRNG control, recorded for a study dump) and compare bytes.
        """
        return {
            "mode": self.mode,
            "hash": self.hash,
            "in_bytes": self.in_bytes,
            "keep_bytes": self.keep_bytes,
            "raw_bytes": len(raw),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
        }


RAW = Conditioning()


def hash_truncate_bytes_needed(out_bytes: int, in_bytes: int, keep_bytes: int) -> int:
    """Raw bytes consumed to produce *out_bytes* of hash_truncate output."""
    blocks = -(-out_bytes // keep_bytes)  # ceil
    return blocks * in_bytes


def hash_truncate(
    raw: bytes, out_bytes: int, hash_name: str, in_bytes: int, keep_bytes: int
) -> bytes:
    """Hash consecutive blocks, keep the head of each digest (normative reference).

    Block i is ``raw[i*in_bytes : (i+1)*in_bytes]``; its contribution is
    ``H(block_i)[:keep_bytes]``. Contributions are concatenated in block
    order and the result is cut to *out_bytes*. No chaining, no counter,
    no key: nothing but the measured block determines its output.
    """
    need = hash_truncate_bytes_needed(out_bytes, in_bytes, keep_bytes)
    if len(raw) < need:
        raise ValueError(
            f"hash_truncate needs {need} input bytes for {out_bytes} output bytes, "
            f"got {len(raw)}"
        )
    view = memoryview(raw)
    out = bytearray()
    for start in range(0, need, in_bytes):
        out += hashlib.new(hash_name, view[start : start + in_bytes]).digest()[:keep_bytes]
    return bytes(out[:out_bytes])


def validate_conditioning(conditioning: Conditioning, config: ConditioningConfig) -> None:
    """Check a selection against the normative rules and the operator's bounds."""
    if conditioning.mode not in CONDITIONING_MODES:
        raise ConditioningError(
            f"conditioning mode must be one of {sorted(CONDITIONING_MODES)}, "
            f"got {conditioning.mode!r}"
        )
    if conditioning.is_raw:
        return
    if not config.allow_request:
        raise ConditioningError(
            "per-request conditioning is disabled on this server "
            "(conditioning.allow_request: false)"
        )
    if conditioning.hash not in config.hashes:
        raise ConditioningError(
            f"conditioning hash must be one of {sorted(config.hashes)}, "
            f"got {conditioning.hash!r}"
        )
    digest_size = CONDITIONING_HASHES[conditioning.hash]
    if not 1 <= conditioning.keep_bytes <= digest_size // 2:
        raise ConditioningError(
            f"keep_bytes must be between 1 and {digest_size // 2} for "
            f"{conditioning.hash} (at least half of every digest is discarded), "
            f"got {conditioning.keep_bytes}"
        )
    if conditioning.in_bytes < 2 * conditioning.keep_bytes:
        raise ConditioningError(
            f"in_bytes must be at least 2 * keep_bytes "
            f"({2 * conditioning.keep_bytes}), got {conditioning.in_bytes}"
        )
    if conditioning.in_bytes > config.max_in_bytes:
        raise ConditioningError(
            f"in_bytes {conditioning.in_bytes} exceeds this server's limit of "
            f"{config.max_in_bytes} (conditioning.max_in_bytes)"
        )


def conditioning_from_metadata(
    metadata: Mapping[str, str | bytes], config: ConditioningConfig
) -> Conditioning:
    """Parse + validate the conditioning headers of one request.

    No header at all is ``raw``. Parameter headers without the mode
    header, or parameters on a ``raw`` request, are refused: a selection
    is either complete or absent, never half-applied.
    """

    def text(key: str) -> str | None:
        value = metadata.get(key)
        if value is None:
            return None
        return value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)

    mode = text(CONDITIONING_HEADER)
    params = {
        key: text(key)
        for key in (
            CONDITIONING_HASH_HEADER,
            CONDITIONING_IN_BYTES_HEADER,
            CONDITIONING_KEEP_BYTES_HEADER,
        )
    }
    given = sorted(k for k, v in params.items() if v is not None)
    if mode is None or mode == "raw":
        if given:
            raise ConditioningError(
                f"conditioning parameter(s) {given} sent without "
                f"{CONDITIONING_HEADER}: hash_truncate"
            )
        return RAW
    if mode not in CONDITIONING_MODES:
        raise ConditioningError(
            f"conditioning mode must be one of {sorted(CONDITIONING_MODES)}, got {mode!r}"
        )

    def number(key: str, default: int) -> int:
        value = params[key]
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            raise ConditioningError(f"{key} must be an integer, got {value!r}") from None

    conditioning = Conditioning(
        mode=mode,
        hash=params[CONDITIONING_HASH_HEADER] or config.default_hash,
        in_bytes=number(CONDITIONING_IN_BYTES_HEADER, config.default_in_bytes),
        keep_bytes=number(CONDITIONING_KEEP_BYTES_HEADER, config.default_keep_bytes),
    )
    validate_conditioning(conditioning, config)
    return conditioning


@dataclass(frozen=True)
class Profile:
    """A validated profile bound to its transform.

    Thin descriptor over a :class:`~qbert0g.config.ProfileConfig`:
    knows how many raw bytes each input must contribute for a given
    output size and applies the transform. Input *reading* (locks,
    pairing, chunk timestamps) is the SourceRouter's job.
    """

    config: ProfileConfig

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def transform(self) -> str:
        return self.config.transform

    @property
    def inputs(self) -> list[str]:
        return self.config.inputs

    @property
    def arity(self) -> int:
        return TRANSFORM_ARITY[self.config.transform]

    def raw_bytes_needed(self, out_bytes: int) -> int:
        """Raw bytes required from EACH input to serve *out_bytes*."""
        if self.config.transform == "parity":
            return parity_bytes_needed(out_bytes, self.config.taps, self.config.stride)
        return out_bytes

    def apply(self, inputs: Sequence[bytes], out_bytes: int) -> bytes:
        """Apply the transform to raw input blocks; returns *out_bytes* bytes."""
        if len(inputs) != self.arity:
            raise ValueError(
                f"profile {self.id!r} ({self.config.transform}) takes "
                f"{self.arity} input(s), got {len(inputs)}"
            )
        if self.config.transform == "identity":
            return identity(inputs[0])
        if self.config.transform == "xnor":
            return xnor(inputs[0], inputs[1])
        return parity(inputs[0], out_bytes, self.config.taps, self.config.stride)
