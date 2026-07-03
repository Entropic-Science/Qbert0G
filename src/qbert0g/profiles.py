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

Layering: imports nothing internal except :mod:`.config`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .config import TRANSFORM_ARITY, ProfileConfig


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
