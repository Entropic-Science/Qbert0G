"""Integrator statistics: goldens, clamping, neff arithmetic, serve-set refusal."""

import math

import pytest

from qbert0g.config import (
    AUX_INTEGRATORS,
    INTEGRATOR_TYPES,
    SERVE_INTEGRATORS,
    Config,
    ConfigError,
)
from qbert0g.fingerprint import Fingerprint, neff_factor_from_acf
from qbert0g.integrators import U_CLAMP, integrate

#: The dragonfly-shaped synthetic baseline used throughout: ACF sum
#: -0.177 => neff_factor = 1 / (1 - 0.354).
NEFF = 1.547987616099071


def _fp(**overrides) -> Fingerprint:
    fields = dict(
        device_id="dragonfly-0",
        firmware="1.7",
        ones_fraction=0.484436,
        byte_mean=122.3525,
        byte_std=73.9,
        bit_acf={1: -0.080, 2: -0.032, 3: -0.031, 4: -0.033, 8: -0.001},
        neff_factor=NEFF,
        quantum_fraction_tier="unrated",
        source_dumps_sha256=("a" * 64,),
        fitted_utc="2026-07-04T00:00:00+00:00",
        session_ref="test",
    )
    fields.update(overrides)
    return Fingerprint(**fields)


#: Fixed 64-byte golden vector: popcount(0..63) = 192 ones of 512 bits.
GOLDEN_RAW = bytes(range(64))


class TestBitZ:
    def test_golden_z_and_u_on_fixed_vector(self):
        # Hand-computed: f = 192/512 = 0.375, f0 = 0.484436,
        # se = sqrt(f0*(1-f0) / (512 * 1.547987616099071)),
        # z = (f - f0)/se, u = 0.5*(1 + erf(z/sqrt(2))).
        result = integrate("bit_z", GOLDEN_RAW, _fp())
        assert result.z == pytest.approx(-6.164806296527517, abs=1e-12)
        assert result.u == pytest.approx(3.5284736243923476e-10, rel=1e-9)
        assert result.aux == {"ones_fraction": 0.375}

    def test_phi_clamped_at_extremes(self):
        fp = _fp(ones_fraction=0.5, neff_factor=1.0, bit_acf={1: 0.0})
        all_ones = integrate("bit_z", b"\xff" * 64, fp)
        all_zeros = integrate("bit_z", b"\x00" * 64, fp)
        assert all_ones.u == 1.0 - U_CLAMP
        assert all_zeros.u == U_CLAMP
        assert all_ones.z == -all_zeros.z > 0

    def test_neff_correction_scales_z_by_sqrt_neff(self):
        # se = sqrt(f0*(1-f0) / (n_bits * neff)): quadrupling neff
        # (more effective samples) doubles |z| for the same block.
        raw = b"\xf0" * 128
        base = integrate("bit_z", raw, _fp(neff_factor=1.0, bit_acf={1: 0.0}))
        corrected = integrate("bit_z", raw, _fp(neff_factor=4.0, bit_acf={1: -0.375}))
        assert corrected.z == pytest.approx(2.0 * base.z, rel=1e-12)


class TestByteZ:
    def test_golden_z_on_fixed_vector(self):
        # mean(0..63) = 31.5; sem = 73.9/sqrt(64)/sqrt(neff);
        # z = (31.5 - 122.3525)/sem — far tail, u clamps to U_CLAMP.
        result = integrate("byte_z", GOLDEN_RAW, _fp())
        assert result.z == pytest.approx(-12.236752382797958, abs=1e-12)
        assert result.u == U_CLAMP
        assert result.aux == {"sample_mean": 31.5}

    def test_matches_qr_sampler_statistic_at_neff_1(self):
        # Continuity: at neff_factor = 1.0 byte_z IS the historical
        # qr-sampler ZScoreMeanAmplifier formula (uint8 mean,
        # sem = population_std/sqrt(n), erf-based CDF).
        fp = _fp(neff_factor=1.0, bit_acf={1: 0.0})
        raw = bytes([120, 130, 125, 127] * 256)
        result = integrate("byte_z", raw, fp)
        samples = list(raw)
        n = len(samples)
        sample_mean = sum(samples) / n
        sem = fp.byte_std / math.sqrt(n)
        z = (sample_mean - fp.byte_mean) / sem
        u = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        assert result.z == pytest.approx(z, abs=1e-12)
        assert result.u == pytest.approx(u, abs=1e-15)

    def test_neff_correction_scales_z_by_sqrt_neff(self):
        raw = bytes([100, 150] * 512)
        base = integrate("byte_z", raw, _fp(neff_factor=1.0, bit_acf={1: 0.0}))
        corrected = integrate("byte_z", raw, _fp(neff_factor=4.0, bit_acf={1: -0.375}))
        assert corrected.z == pytest.approx(2.0 * base.z, rel=1e-12)


class TestNeffArithmetic:
    def test_neff_factor_from_acf_matches_golden(self):
        assert neff_factor_from_acf(
            {1: -0.080, 2: -0.032, 3: -0.031, 4: -0.033, 8: -0.001}
        ) == pytest.approx(NEFF, abs=1e-15)

    def test_positive_acf_shrinks_effective_sample_size(self):
        assert neff_factor_from_acf({1: 0.25}) == pytest.approx(1.0 / 1.5)

    def test_degenerate_acf_sum_refused(self):
        with pytest.raises(ConfigError, match="effective sample size"):
            neff_factor_from_acf({1: -0.5, 2: -0.25})


class TestAuxIntegrators:
    # b"\xf0\x0f" = 1111 0000 0000 1111 (16 bits).
    def test_cusum_golden(self):
        # Centered on f0 = 0.25: walk peaks at |4.0| (all-ones tail),
        # normalized by sqrt(16 * 0.25 * 0.75) = sqrt(3).
        result = integrate("cusum", b"\xf0\x0f", _fp(ones_fraction=0.25))
        assert result.z == pytest.approx(4.0 / math.sqrt(3.0), abs=1e-12)
        assert result.aux["cusum"] == result.z

    def test_rw_excursion_golden(self):
        # Raw ±1 walk peaks at |4| after the leading ones; /sqrt(16).
        result = integrate("rw_excursion", b"\xf0\x0f", _fp())
        assert result.z == pytest.approx(1.0, abs=1e-12)
        assert result.aux["rw_excursion"] == 1.0

    def test_cusum_sees_intermittent_push_the_mean_misses(self):
        # First half all-ones, second half all-zeros: block mean is
        # exactly f0 = 0.5 (bit_z z == 0) but the path excursion is huge.
        raw = b"\xff" * 32 + b"\x00" * 32
        fp = _fp(ones_fraction=0.5, neff_factor=1.0, bit_acf={1: 0.0})
        assert integrate("bit_z", raw, fp).z == pytest.approx(0.0, abs=1e-12)
        assert integrate("cusum", raw, fp).z == pytest.approx(
            128.0 / math.sqrt(512 * 0.25), abs=1e-12
        )


class TestOfflineIntegrators:
    def test_majority_vote_both_directions(self):
        fp = _fp(ones_fraction=0.5)
        up = integrate("majority_vote", b"\xff\xf0", fp)
        down = integrate("majority_vote", b"\x00\x0f", fp)
        assert (up.aux["majority"], up.z, up.u) == (1.0, 1.0, 1.0 - U_CLAMP)
        assert (down.aux["majority"], down.z, down.u) == (0.0, -1.0, U_CLAMP)

    def test_kmer_mode_finds_modal_byte(self):
        raw = bytes([7, 7, 7, 7, 1, 2, 3, 4])
        result = integrate("kmer_mode", raw, _fp())
        assert result.aux == {"mode_byte": 7.0, "mode_fraction": 0.5}
        assert (result.z, result.u) == (0.0, 0.5)  # inert: never a served signal


class TestDispatch:
    def test_unknown_integrator_refused(self):
        with pytest.raises(ValueError, match="unknown integrator"):
            integrate("sha256", b"\x00", _fp())

    def test_empty_block_refused(self):
        with pytest.raises(ValueError, match="empty"):
            integrate("bit_z", b"", _fp())

    def test_every_known_name_dispatches(self):
        for name in INTEGRATOR_TYPES:
            result = integrate(name, GOLDEN_RAW, _fp())
            assert 0.0 < result.u < 1.0

    def test_integrator_sets_are_consistent(self):
        assert SERVE_INTEGRATORS <= INTEGRATOR_TYPES
        assert AUX_INTEGRATORS <= INTEGRATOR_TYPES
        assert not SERVE_INTEGRATORS & AUX_INTEGRATORS


def _config(**integration) -> dict:
    return {
        "devices": [{"id": "mock-0", "type": "mock", "fingerprint": "/fp/mock-0.json"}],
        "integration": integration,
    }


class TestServeSetConfig:
    """The serve path refuses non-serve integrators at startup (FR-Q2)."""

    def test_default_integrator_kmer_mode_is_a_config_error(self):
        with pytest.raises(ConfigError, match="not a serve-path integrator"):
            Config.from_dict(_config(default_integrator="kmer_mode"))

    def test_aux_integrators_are_not_servable_either(self):
        for name in ("cusum", "rw_excursion", "majority_vote"):
            with pytest.raises(ConfigError, match="not a serve-path integrator"):
                Config.from_dict(_config(default_integrator=name))

    def test_unknown_default_integrator_rejected(self):
        with pytest.raises(ConfigError, match="default_integrator"):
            Config.from_dict(_config(default_integrator="mean"))

    def test_serve_integrators_accepted(self):
        for name in sorted(SERVE_INTEGRATORS):
            config = Config.from_dict(_config(default_integrator=name))
            assert config.integration.default_integrator == name

    def test_secondaries_must_be_aux(self):
        config = Config.from_dict(_config(secondaries=["cusum", "rw_excursion"]))
        assert config.integration.secondaries == ["cusum", "rw_excursion"]
        with pytest.raises(ConfigError, match="secondaries"):
            Config.from_dict(_config(secondaries=["bit_z"]))
        with pytest.raises(ConfigError, match="secondaries"):
            Config.from_dict(_config(secondaries=["kmer_mode"]))

    def test_defaults(self):
        config = Config.from_dict({"devices": [{"id": "mock-0", "type": "mock"}]})
        assert config.integration.block_bytes == 2_097_152
        assert config.integration.default_integrator == "bit_z"
        assert config.integration.secondaries == []
        assert config.integration.sources == []
