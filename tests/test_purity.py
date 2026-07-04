"""Purity taxonomy: derivation matrix over all axes + canonical round-trip."""

import pytest

from qbert0g.config import ConfigError
from qbert0g.fingerprint import Fingerprint
from qbert0g.purity import (
    EntropyLabel,
    Integrity,
    Origin,
    Processing,
    derive_static_label,
    resolve_request_bits,
)


def _fp(tier: str = "unrated") -> Fingerprint:
    return Fingerprint(
        device_id="dragonfly-0",
        firmware="1.7",
        ones_fraction=0.484436,
        byte_mean=122.3525,
        byte_std=73.9,
        bit_acf={1: -0.080, 2: -0.032, 3: -0.031, 4: -0.033, 8: -0.001},
        neff_factor=1.547987616099071,
        quantum_fraction_tier=tier,
        source_dumps_sha256=("ab" * 32,),
        fitted_utc="2026-07-04T00:00:00+00:00",
        session_ref="test",
    )


class TestDeviceLabels:
    @pytest.mark.parametrize(
        ("device_type", "mode", "origin", "integrity", "processing"),
        [
            ("firefly", "raw", Origin.QUANTUM, Integrity.INTACT, Processing.RAW),
            ("dragonfly", "raw_samples", Origin.QUANTUM, Integrity.INTACT, Processing.RAW),
            ("qcicada", "sha256", Origin.QUANTUM, Integrity.SCRAMBLED, Processing.UNIFORM),
            ("chardev", None, Origin.QUANTUM, Integrity.INTACT, Processing.RAW),
            ("mock", "raw", Origin.TRUE_RANDOM, Integrity.INTACT, Processing.RAW),
            ("mock", "sha256", Origin.TRUE_RANDOM, Integrity.SCRAMBLED, Processing.UNIFORM),
        ],
    )
    def test_device_matrix(self, device_type, mode, origin, integrity, processing):
        label = derive_static_label("device", device_type, mode)
        assert label.origin is origin
        assert label.integrity is integrity
        assert label.processing is processing
        assert label.expanded is False  # no DRBG exists in this server
        assert label.amplified is False and label.quantum_verified is False

    def test_fingerprint_supplies_the_tier(self):
        assert derive_static_label("device", "dragonfly", "raw").quantum_fraction_tier == "unrated"
        label = derive_static_label("device", "dragonfly", "raw", fingerprint=_fp("98+"))
        assert label.quantum_fraction_tier == "98+"

    def test_device_type_required(self):
        with pytest.raises(ValueError, match="device_type"):
            derive_static_label("device")


class TestControlLabels:
    def test_controls_are_pseudo_intact_raw(self):
        label = derive_static_label("control")
        assert (label.origin, label.integrity, label.processing) == (
            Origin.PSEUDO,
            Integrity.INTACT,
            Processing.RAW,
        )

    def test_control_fingerprint_tier_passes_through(self):
        assert (
            derive_static_label("control", fingerprint=_fp("90+")).quantum_fraction_tier == "90+"
        )


class TestProfileLabels:
    def _device(self, tier="unrated", mode="raw", device_type="dragonfly"):
        return derive_static_label("device", device_type, mode, fingerprint=_fp(tier))

    def test_xnor_of_two_intact_quantum_stays_quantum_intact_raw(self):
        label = derive_static_label(
            "profile", input_labels=(self._device("99+"), self._device("99+"))
        )
        assert (label.origin, label.integrity, label.processing) == (
            Origin.QUANTUM,
            Integrity.INTACT,
            Processing.RAW,
        )
        assert label.quantum_fraction_tier == "99+"

    def test_any_pseudo_input_makes_the_profile_pseudo(self):
        label = derive_static_label(
            "profile", input_labels=(self._device(), derive_static_label("control"))
        )
        assert label.origin is Origin.PSEUDO

    def test_true_random_beats_quantum_in_the_worst_case(self):
        mock = derive_static_label("device", "mock", "raw")
        label = derive_static_label("profile", input_labels=(self._device(), mock))
        assert label.origin is Origin.TRUE_RANDOM

    def test_any_scrambled_input_scrambles_the_profile(self):
        # sha256 anywhere in the path => scrambled; the transform
        # itself (xnor/parity: invertible-arithmetic-class) keeps intact.
        scrambled = self._device(mode="sha256")
        label = derive_static_label("profile", input_labels=(self._device(), scrambled))
        assert label.integrity is Integrity.SCRAMBLED
        assert label.processing is Processing.UNIFORM  # most-processed input wins

    def test_tier_is_the_worst_input_tier(self):
        label = derive_static_label(
            "profile", input_labels=(self._device("99+"), self._device("unrated"))
        )
        assert label.quantum_fraction_tier == "unrated"
        label = derive_static_label(
            "profile", input_labels=(self._device("95+"), self._device("98+"))
        )
        assert label.quantum_fraction_tier == "95+"

    def test_profile_requires_inputs(self):
        with pytest.raises(ValueError, match="input_labels"):
            derive_static_label("profile")

    def test_unknown_kind_refused(self):
        with pytest.raises(ValueError, match="source_kind"):
            derive_static_label("oracle")


class TestCanonical:
    def test_exact_canonical_string(self):
        label = EntropyLabel(
            origin=Origin.QUANTUM,
            integrity=Integrity.INTACT,
            processing=Processing.RAW,
            expanded=False,
            quantum_fraction_tier="unrated",
            amplified=True,
            integration_n=2_097_152,
            quantum_verified=True,
        )
        assert label.canonical() == "quantum/intact/raw/amplified:2097152/qf:unrated/QV"

    def test_static_label_canonical_has_no_optional_tokens(self):
        label = derive_static_label("control")
        assert label.canonical() == "pseudo/intact/raw/qf:unrated"

    def test_expanded_token_position(self):
        label = EntropyLabel(
            origin=Origin.TRUE_RANDOM,
            integrity=Integrity.SCRAMBLED,
            processing=Processing.UNIFORM,
            expanded=True,
            quantum_fraction_tier="90+",
        )
        assert label.canonical() == "true_random/scrambled/uniform/expanded/qf:90+"

    @pytest.mark.parametrize(
        "label",
        [
            EntropyLabel(Origin.QUANTUM, Integrity.INTACT, Processing.RAW, False, "unrated"),
            EntropyLabel(Origin.PSEUDO, Integrity.INTACT, Processing.RAW, False, "unrated"),
            EntropyLabel(Origin.QUANTUM, Integrity.SCRAMBLED, Processing.UNIFORM, True, "99+"),
            EntropyLabel(
                Origin.QUANTUM,
                Integrity.INTACT,
                Processing.RAW,
                False,
                "98+",
                amplified=True,
                integration_n=2_097_152,
                quantum_verified=True,
            ),
            EntropyLabel(
                Origin.TRUE_RANDOM,
                Integrity.INTACT,
                Processing.DEBIASED,
                True,
                "95+",
                amplified=True,
                integration_n=1024,
            ),
        ],
    )
    def test_round_trip(self, label):
        assert EntropyLabel.from_canonical(label.canonical()) == label

    def test_from_canonical_rejects_garbage(self):
        for bad in ("", "quantum", "quantum/intact", "hopeium/intact/raw/qf:unrated",
                    "quantum/intact/raw", "quantum/intact/raw/qf:unrated/glowing"):
            with pytest.raises(ValueError):
                EntropyLabel.from_canonical(bad)

    def test_bad_tier_refused_at_construction(self):
        with pytest.raises(ConfigError, match="quantum_fraction_tier"):
            EntropyLabel(Origin.QUANTUM, Integrity.INTACT, Processing.RAW, False, "100")


class TestRequestBits:
    def test_resolve_request_bits_copies_not_mutates(self):
        static = derive_static_label("device", "dragonfly", "raw", fingerprint=_fp("98+"))
        served = resolve_request_bits(static, integration_n=2_097_152, quantum_verified=True)
        assert served.amplified is True
        assert served.integration_n == 2_097_152
        assert served.quantum_verified is True
        assert served.canonical() == "quantum/intact/raw/amplified:2097152/qf:98+/QV"
        # The static label is untouched (frozen dataclass, replace()).
        assert static.amplified is False and static.integration_n == 0
