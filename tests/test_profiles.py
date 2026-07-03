"""Transform library: normative semantics, golden vectors, statistical smoke.

Statistical assertions here run ONLY against synthetic inputs with
known constructed distributions (unit tests of the transform math) —
never as gates on what a configured source serves.
"""

import numpy as np
import pytest

from qbert0g.config import TRANSFORM_ARITY, ProfileConfig
from qbert0g.profiles import (
    Profile,
    identity,
    parity,
    parity_bits_needed,
    parity_bytes_needed,
    xnor,
)

# ── golden vectors ───────────────────────────────────────────────────────
# Generated ONCE from the spec's normative reference snippet (verbatim)
# and committed as literals; the implementation must match bit-for-bit.
_GOLDEN_RAW = bytes.fromhex(
    "8b084fe4ef1beea64499314ebce9135f64e90ee2fc6d634a37fe771ab4bd828e"
    "ac4a92a8a46d8595214f1593978bcec63d5b3d454975e0bf6637661512fa3e58"
)
_GOLDEN = [
    # (out_bytes, taps, stride, expected_hex)
    (4, [0, 9, 19, 30], 4, "c16e275c"),  # the canonical phase-covering design
    (8, [0, 1], 3, "c80779b48f8e65e5"),
    (2, [0, 5, 11], 7, "b43f"),
]
_GOLDEN_SEQ = ("92c7c798", bytes(range(32)))  # parity(0..31, 4, [0,9,19,30], 4)


class TestIdentity:
    def test_passthrough(self):
        assert identity(b"\x00\xff\x42") == b"\x00\xff\x42"


class TestXnor:
    def test_agreement_with_self_is_all_ones(self):
        x = bytes(range(256))
        assert xnor(x, x) == b"\xff" * 256

    def test_agreement_with_complement_is_all_zeros(self):
        x = bytes(range(256))
        inv = bytes(b ^ 0xFF for b in x)
        assert xnor(x, inv) == b"\x00" * 256

    def test_bytewise_semantics(self):
        # output byte i = ~(a[i] ^ b[i]) & 0xFF
        assert xnor(b"\xb5", b"\xa3") == bytes([~(0xB5 ^ 0xA3) & 0xFF])

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="equal length"):
            xnor(b"\x00\x00", b"\x00")


class TestParity:
    def test_consumption_formula(self):
        # need = stride*(M-1) + max(taps) + 1 input bits
        assert parity_bits_needed(1, [0, 9, 19, 30], 4) == 4 * 7 + 30 + 1
        assert parity_bits_needed(4, [0, 9, 19, 30], 4) == 4 * 31 + 30 + 1
        # For the default params: ceil((4M + 27) / 8) input bytes.
        for out_bytes in (1, 2, 16, 100):
            m = out_bytes * 8
            assert parity_bytes_needed(out_bytes, [0, 9, 19, 30], 4) == (4 * m + 27 + 7) // 8

    def test_golden_vectors(self):
        for out_bytes, taps, stride, expected in _GOLDEN:
            assert parity(_GOLDEN_RAW, out_bytes, taps, stride).hex() == expected

    def test_golden_vector_sequential_input(self):
        expected, raw = _GOLDEN_SEQ
        assert parity(raw, 4, [0, 9, 19, 30], 4).hex() == expected

    def test_bit_order_is_msb_first(self):
        # NORMATIVE bit order: numpy unpackbits default — MSB first
        # within each byte. Bit 0 of the stream is the MSB of byte 0.
        assert np.unpackbits(np.array([0b1000_0000], dtype=np.uint8))[0] == 1
        # taps=[0], stride=8: output bit i = input bit 8i = MSB of byte i.
        raw = bytes([0x80, 0x00, 0xFF, 0x7F, 0x80, 0x00, 0xFF, 0x7F])
        assert parity(raw, 1, [0], 8) == bytes([0b1010_1010])

    def test_single_tap_stride_one_is_bitwise_identity(self):
        raw = b"\xa5\x3c\xf0\x0f"
        assert parity(raw, 4, [0], 1) == raw

    def test_insufficient_input_rejected(self):
        with pytest.raises(ValueError, match="input bits"):
            parity(b"\x00", 1, [0, 9, 19, 30], 4)


class TestStatisticalSmoke:
    """Piling-up behavior on KNOWN constructed biased inputs (seeded)."""

    @staticmethod
    def _biased_bytes(n_bits: int, p_one: float, seed: int) -> bytes:
        rng = np.random.default_rng(seed)
        bits = (rng.integers(0, 100, size=n_bits, dtype=np.uint8) < int(p_one * 100)).astype(
            np.uint8
        )
        return np.packbits(bits).tobytes()

    @staticmethod
    def _p_one(data: bytes) -> float:
        return float(np.unpackbits(np.frombuffer(data, np.uint8)).mean())

    def test_parity4_debias(self):
        # P(1)=0.55 input through parity-4: piling-up bias 2^3 * 0.05^4
        # = 5e-5; |P(1) - 0.5| < 0.002 on 1 MB of output.
        out_bytes = 1 << 20
        taps, stride = [0, 9, 19, 30], 4
        raw = self._biased_bytes(parity_bits_needed(out_bytes, taps, stride), 0.55, seed=1)
        out = parity(raw, out_bytes, taps, stride)
        assert abs(self._p_one(out) - 0.5) < 0.002

    def test_xnor_bias_below_product_bound(self):
        # Two independent streams with per-bit P(1) = 0.5 + eps agree with
        # probability 0.5 + 2*eps^2 — the output bias (0.005) is bounded
        # by the PRODUCT of the input biases (0.05 * 0.05 * 2), far below
        # either input's own bias (0.05).
        n_bits = 1 << 23  # 1 MB
        a = self._biased_bytes(n_bits, 0.55, seed=2)
        b = self._biased_bytes(n_bits, 0.55, seed=3)
        p = self._p_one(xnor(a, b))
        assert abs(p - 0.505) < 0.002  # matches the piling-up expectation
        assert abs(p - 0.5) < 0.0075  # and stays below the product bound


class TestProfileDescriptor:
    @staticmethod
    def _profile(transform: str, inputs: list[str], **params) -> Profile:
        return Profile(
            ProfileConfig(id="p0", transform=transform, inputs=inputs, **params)
        )

    def test_arity_matches_config_map(self):
        assert self._profile("identity", ["a"]).arity == TRANSFORM_ARITY["identity"] == 1
        assert self._profile("xnor", ["a", "b"]).arity == TRANSFORM_ARITY["xnor"] == 2
        assert (
            self._profile("parity", ["a"], taps=(0, 1), stride=1).arity
            == TRANSFORM_ARITY["parity"]
            == 1
        )

    def test_raw_bytes_needed(self):
        assert self._profile("identity", ["a"]).raw_bytes_needed(100) == 100
        assert self._profile("xnor", ["a", "b"]).raw_bytes_needed(100) == 100
        parity_profile = self._profile("parity", ["a"], taps=(0, 9, 19, 30), stride=4)
        assert parity_profile.raw_bytes_needed(4) == parity_bytes_needed(4, [0, 9, 19, 30], 4)

    def test_apply_dispatch(self):
        raw = bytes(range(32))
        assert self._profile("identity", ["a"]).apply([raw], 32) == raw
        assert self._profile("xnor", ["a", "b"]).apply([raw, raw], 32) == b"\xff" * 32
        parity_profile = self._profile("parity", ["a"], taps=(0, 9, 19, 30), stride=4)
        assert parity_profile.apply([raw], 4) == parity(raw, 4, [0, 9, 19, 30], 4)

    def test_apply_wrong_input_count_rejected(self):
        with pytest.raises(ValueError, match="input"):
            self._profile("xnor", ["a", "b"]).apply([b"\x00"], 1)
