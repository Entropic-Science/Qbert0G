"""Fingerprint load/validate/hash + fit-script round-trip (FR-Q3).

The fit script is NOT part of the server import graph; tests load it
from its file path (the fit_markov convention).
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from qbert0g.config import Config, ConfigError
from qbert0g.fingerprint import (
    Fingerprint,
    load_config_fingerprints,
    load_fingerprint,
)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "fit_fingerprint.py"
_spec = importlib.util.spec_from_file_location("fit_fingerprint", _SCRIPT)
fit_fingerprint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fit_fingerprint)

#: A consistent baseline: ACF sum -0.177 => neff = 1/(1 - 0.354).
VALID = {
    "device_id": "dragonfly-0",
    "firmware": "1.7",
    "ones_fraction": 0.484436,
    "byte_mean": 122.3525,
    "byte_std": 73.9,
    "bit_acf": {"1": -0.080, "2": -0.032, "3": -0.031, "4": -0.033, "8": -0.001},
    "neff_factor": 1.547987616099071,
    "quantum_fraction_tier": "unrated",
    "source_dumps_sha256": ["ab" * 32],
    "fitted_utc": "2026-07-04T00:00:00+00:00",
    "session_ref": "qrng_soak_20260703_191501",
}


def _write(tmp_path: Path, **overrides) -> Path:
    data = dict(VALID)
    data.update(overrides)
    for key in [k for k, v in overrides.items() if v is None]:
        del data[key]
    path = tmp_path / "fp.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestLoadValidate:
    def test_valid_file_loads_with_hash(self, tmp_path):
        path = _write(tmp_path)
        fp, sha256 = load_fingerprint(str(path))
        assert isinstance(fp, Fingerprint)
        assert fp.device_id == "dragonfly-0"
        assert fp.ones_fraction == 0.484436
        assert fp.byte_std == 73.9
        assert fp.bit_acf == {1: -0.080, 2: -0.032, 3: -0.031, 4: -0.033, 8: -0.001}
        assert fp.neff_factor == 1.547987616099071
        assert fp.quantum_fraction_tier == "unrated"
        assert fp.source_dumps_sha256 == ("ab" * 32,)
        assert sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_missing_file_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_fingerprint(str(tmp_path / "nope.json"))

    def test_invalid_json_refused(self, tmp_path):
        path = tmp_path / "fp.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="JSON"):
            load_fingerprint(str(path))

    def test_missing_key_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="missing key.*byte_std"):
            load_fingerprint(str(_write(tmp_path, byte_std=None)))

    def test_unknown_key_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown key"):
            load_fingerprint(str(_write(tmp_path, adaptive=True)))

    def test_neff_cross_check_refuses_edited_file(self, tmp_path):
        # A hand-edited neff_factor inconsistent with the stored ACF
        # (beyond 1e-6) is refused — baselines are frozen artifacts.
        path = _write(tmp_path, neff_factor=1.549)
        with pytest.raises(ConfigError, match="does not match the stored bit_acf"):
            load_fingerprint(str(path))

    def test_neff_cross_check_tolerates_1e6(self, tmp_path):
        path = _write(tmp_path, neff_factor=VALID["neff_factor"] + 5e-7)
        fp, _ = load_fingerprint(str(path))
        assert fp.neff_factor == pytest.approx(VALID["neff_factor"], abs=1e-6)

    def test_out_of_range_values_refused(self, tmp_path):
        for overrides, match in [
            ({"ones_fraction": 0.0}, "ones_fraction"),
            ({"ones_fraction": 1.5}, "ones_fraction"),
            ({"byte_mean": 300.0}, "byte_mean"),
            ({"byte_std": 0.0}, "byte_std"),
            ({"quantum_fraction_tier": "100"}, "quantum_fraction_tier"),
            ({"source_dumps_sha256": ["nothex"]}, "source_dumps_sha256"),
            ({"neff_factor": "fast"}, "neff_factor"),
        ]:
            with pytest.raises(ConfigError, match=match):
                load_fingerprint(str(_write(tmp_path, **overrides)))

    def test_bad_acf_refused(self, tmp_path):
        for bad_acf in ({}, {"x": 0.1}, {"0": 0.1}, {"1": 1.5}, "nope"):
            with pytest.raises(ConfigError, match="bit_acf"):
                load_fingerprint(str(_write(tmp_path, bit_acf=bad_acf)))

    def test_degenerate_acf_sum_refused(self, tmp_path):
        # sum(rho) <= -0.5 leaves no positive effective sample size.
        path = _write(tmp_path, bit_acf={"1": -0.6}, neff_factor=1.0)
        with pytest.raises(ConfigError, match="effective sample size"):
            load_fingerprint(str(path))


class TestStartupRule:
    """FR-Q3: drawable sources REQUIRE a valid fingerprint at startup."""

    def _config_dict(self, fingerprint: str) -> dict:
        device = {"id": "mock-0", "type": "mock"}
        if fingerprint:
            device["fingerprint"] = fingerprint
        return {"devices": [device], "integration": {"sources": ["mock-0"]}}

    def test_source_without_fingerprint_path_is_a_config_error(self):
        with pytest.raises(ConfigError, match="no `fingerprint:` path"):
            Config.from_dict(self._config_dict(""))

    def test_source_not_a_device_or_control_rejected(self):
        with pytest.raises(ConfigError, match="not a configured device"):
            Config.from_dict(
                {
                    "devices": [{"id": "mock-0", "type": "mock"}],
                    "integration": {"sources": ["ghost"]},
                }
            )

    def test_missing_fingerprint_file_is_a_startup_error(self, tmp_path):
        config = Config.from_dict(self._config_dict(str(tmp_path / "nope.json")))
        with pytest.raises(ConfigError, match="mock-0.*not found"):
            load_config_fingerprints(config)

    def test_invalid_fingerprint_file_is_a_startup_error(self, tmp_path):
        path = _write(tmp_path, neff_factor=2.0)
        config = Config.from_dict(self._config_dict(str(path)))
        with pytest.raises(ConfigError, match="mock-0"):
            load_config_fingerprints(config)

    def test_valid_fingerprints_load_by_source_id(self, tmp_path):
        path = _write(tmp_path)
        config = Config.from_dict(self._config_dict(str(path)))
        loaded = load_config_fingerprints(config)
        fp, sha256 = loaded["mock-0"]
        assert fp.device_id == "dragonfly-0"
        assert sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_control_fingerprints_work_too(self, tmp_path):
        path = _write(tmp_path)
        config = Config.from_dict(
            {
                "devices": [{"id": "mock-0", "type": "mock"}],
                "controls": [
                    {
                        "id": "prng-uniform-0",
                        "type": "prng_uniform",
                        "seed": "0x9e3779b97f4a7c15f39cc0605cedc834",
                        "fingerprint": str(path),
                    }
                ],
                "integration": {"sources": ["prng-uniform-0"]},
            }
        )
        assert "prng-uniform-0" in load_config_fingerprints(config)


class TestFitScript:
    """scripts/fit_fingerprint.py: fit-vs-dump tolerance + sign-off output."""

    def _fit(self, tmp_path: Path, dump: bytes, *extra: str) -> Path:
        dump_file = tmp_path / "dump.bin"
        dump_file.write_bytes(dump)
        out = tmp_path / "fitted.json"
        fit_fingerprint.main(
            [str(dump_file), "--out", str(out), "--device-id", "dragonfly-0", *extra]
        )
        return out

    def test_fitted_file_loads_and_tracks_dump_statistics(self, tmp_path):
        dump = np.random.default_rng(42).integers(0, 256, 200_000, np.uint8).tobytes()
        out = self._fit(tmp_path, dump)
        # The written JSON passes the loader's full validation,
        # INCLUDING the neff-vs-ACF cross-check.
        fp, sha256 = load_fingerprint(str(out))
        assert len(sha256) == 64
        # A uniform dump: f ~ 0.5, byte mean ~ 127.5, std ~ 73.9, ACF ~ 0.
        assert abs(fp.ones_fraction - 0.5) < 0.005
        assert abs(fp.byte_mean - 127.5) < 1.0
        assert abs(fp.byte_std - 73.9) < 1.0
        assert set(fp.bit_acf) == {1, 2, 3, 4, 8}
        for rho in fp.bit_acf.values():
            assert abs(rho) < 0.01
        assert abs(fp.neff_factor - 1.0) < 0.05
        assert fp.quantum_fraction_tier == "unrated"
        assert fp.source_dumps_sha256 == (hashlib.sha256(dump).hexdigest(),)
        assert fp.device_id == "dragonfly-0"
        assert fp.firmware == "" and fp.session_ref == ""

    def test_biased_dump_shifts_ones_fraction(self, tmp_path):
        # Bytes drawn from {0x00, 0xff} with P(0xff)=0.25: f = 0.25.
        rng = np.random.default_rng(7)
        dump = np.where(rng.random(100_000) < 0.25, 0xFF, 0x00).astype(np.uint8).tobytes()
        fp, _ = load_fingerprint(str(self._fit(tmp_path, dump)))
        assert abs(fp.ones_fraction - 0.25) < 0.01

    def test_metadata_flags_are_stored(self, tmp_path):
        dump = bytes(range(256)) * 64
        out = self._fit(
            tmp_path, dump, "--firmware", "1.7", "--session", "soak-1", "--tier", "95+"
        )
        fp, _ = load_fingerprint(str(out))
        assert fp.firmware == "1.7"
        assert fp.session_ref == "soak-1"
        assert fp.quantum_fraction_tier == "95+"
        assert fp.fitted_utc  # ISO timestamp present

    def test_prints_side_by_side_comparison(self, tmp_path, capsys):
        self._fit(tmp_path, np.random.default_rng(1).bytes(50_000))
        out = capsys.readouterr().out
        assert "dumps" in out and "stored" in out
        assert "ones fraction" in out
        assert "neff factor" in out
        assert "bit ACF lag 8" in out

    def test_acf_never_crosses_dump_boundaries(self, tmp_path):
        # Two constant-but-opposite dumps: within each dump the centered
        # bits are constant, so lagged products are positive; across the
        # boundary they would be negative. rho must be exactly the
        # boundary-free value.
        d1 = tmp_path / "a.bin"
        d2 = tmp_path / "b.bin"
        d1.write_bytes(b"\xff" * 1000)
        d2.write_bytes(b"\x00" * 1000)
        out = tmp_path / "fitted.json"
        fit_fingerprint.main([str(d1), str(d2), "--out", str(out), "--device-id", "d0"])
        stored = json.loads(out.read_text(encoding="utf-8"))
        # Global f = 0.5, per-dump centered bits are ±0.5 constants:
        # every within-dump lagged product is +0.25 => rho ~ +1 (not
        # dragged down by cross-boundary terms).
        assert stored["bit_acf"]["1"] > 0.99
