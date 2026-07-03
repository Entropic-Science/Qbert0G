"""scripts/fit_markov.py: model round-trip within smoothing tolerance.

The fit script is NOT part of the server import graph; tests load it
from its file path.
"""

import importlib.util
import json
from pathlib import Path

import numpy as np

from qbert0g.config import ControlConfig
from qbert0g.controls import PrngMarkovControl, load_markov_model

_SCRIPT = Path(__file__).parent.parent / "scripts" / "fit_markov.py"
_spec = importlib.util.spec_from_file_location("fit_markov", _SCRIPT)
fit_markov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fit_markov)

SEED = "0x9e3779b97f4a7c15f39cc0605cedc834"


def _true_model() -> tuple[np.ndarray, np.ndarray]:
    """A chain confined to bytes 0..7 so a small dump visits every used row."""
    initial = np.zeros(256)
    initial[:8] = 1 / 8
    transition = np.full((256, 256), 1 / 256)  # unvisited rows: uniform filler
    transition[:8] = 0.0
    for x in range(8):
        transition[x, (x + 1) % 8] = 0.6
        transition[x, (x + 3) % 8] = 0.4
    return initial, transition


def _synthetic_dump(tmp_path: Path, n_bytes: int = 200_000) -> tuple[bytes, np.ndarray]:
    """Generate a dump from the known model via the markov control itself."""
    initial, transition = _true_model()
    model_path = tmp_path / "true.npz"
    np.savez(model_path, initial=initial, transition=transition, meta="{}")
    control = PrngMarkovControl(
        ControlConfig(id="gen", type="prng_markov", seed=SEED, model=str(model_path))
    )
    return control.read(n_bytes), transition


class TestFitRoundTrip:
    def test_recovers_known_transition_within_smoothing_tolerance(self, tmp_path):
        dump, true_transition = _synthetic_dump(tmp_path)
        _, fitted = fit_markov.fit([dump])
        # Rows 0..7 are each visited ~25k times: Laplace +1 smoothing
        # distorts by ~256/(N+256) ≈ 0.01 and sampling noise is smaller.
        assert float(np.max(np.abs(fitted[:8] - true_transition[:8]))) < 0.03
        # Fitted rows are exactly stochastic.
        assert np.allclose(fitted.sum(axis=1), 1.0)

    def test_transitions_not_counted_across_dump_boundaries(self):
        # Two one-byte dumps have NO transitions; rows stay uniform.
        _, fitted = fit_markov.fit([b"\x05", b"\x09"])
        assert np.allclose(fitted, 1 / 256)

    def test_written_model_loads_as_control_model(self, tmp_path):
        dump, _ = _synthetic_dump(tmp_path, n_bytes=10_000)
        dump_file = tmp_path / "dump.bin"
        dump_file.write_bytes(dump)
        out = tmp_path / "fitted.npz"
        fit_markov.main(
            [str(dump_file), "--out", str(out), "--device-id", "dragonfly-0"]
        )
        initial, transition, meta, sha256 = load_markov_model(str(out))
        assert initial.shape == (256,)
        assert transition.shape == (256, 256)
        assert meta["source_device_id"] == "dragonfly-0"
        assert meta["laplace_smoothing"] == 1.0
        assert meta["firmware_version"] == "" and meta["session_id"] == ""
        assert len(meta["dump_sha256"]) == 1 and len(meta["dump_sha256"][0]) == 64
        assert len(sha256) == 64
        # The fitted model is directly servable by a prng_markov control.
        control = PrngMarkovControl(
            ControlConfig(id="fit", type="prng_markov", seed=SEED, model=str(out))
        )
        assert len(control.read(64)) == 64

    def test_fingerprints_agree_between_model_and_dump(self):
        # A full-support dump (realistic QRNG shape): the smoothing tail
        # is negligible, so the analytic model fingerprint must track the
        # empirical dump fingerprint. (A chain confined to a few small
        # byte values would NOT show this — smoothing mass on far-away
        # values dominates the value-based lag-1 correlation.)
        dump = np.random.default_rng(42).integers(0, 256, 200_000, np.uint8).tobytes()
        initial, transition = fit_markov.fit([dump])
        model_fp = fit_markov.fingerprint_from_model(initial, transition)
        dump_fp = fit_markov.fingerprint_from_dumps([dump])
        assert abs(model_fp["byte_mean"] - dump_fp["byte_mean"]) < 1.0
        assert abs(model_fp["lag1_corr"] - dump_fp["lag1_corr"]) < 0.02
        for m, d in zip(model_fp["bit_p1"], dump_fp["bit_p1"], strict=True):
            assert abs(m - d) < 0.01

    def test_meta_is_json_string_in_npz(self, tmp_path):
        dump_file = tmp_path / "dump.bin"
        dump_file.write_bytes(bytes(range(256)) * 40)
        out = tmp_path / "fitted.npz"
        fit_markov.main([str(dump_file), "--out", str(out), "--device-id", "d0"])
        with np.load(out) as npz:
            meta = json.loads(str(npz["meta"]))
        assert "fit_date" in meta
