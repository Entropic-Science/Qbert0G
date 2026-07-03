"""PRNG control sources: reproducibility, offset regeneration, model validation."""

import json
import logging
from pathlib import Path

import numpy as np
import pytest

from qbert0g.config import ConfigError, ControlConfig
from qbert0g.controls import (
    PrngMarkovControl,
    PrngUniformControl,
    load_markov_model,
    make_control,
    markov_stream_bytes,
    uniform_stream_bytes,
)

SEED = "0x9e3779b97f4a7c15f39cc0605cedc834"
SEED2 = "0x243f6a8885a308d313198a2e03707344"


def _uniform_config(**overrides) -> ControlConfig:
    kwargs = {"id": "prng-uniform-0", "type": "prng_uniform", "seed": SEED}
    kwargs.update(overrides)
    return ControlConfig(**kwargs)


def _write_model(
    path: Path,
    initial: np.ndarray | None = None,
    transition: np.ndarray | None = None,
    meta: dict | None = None,
) -> str:
    if initial is None:
        initial = np.full(256, 1 / 256)
    if transition is None:
        transition = np.full((256, 256), 1 / 256)
    np.savez(
        path,
        initial=initial,
        transition=transition,
        meta=json.dumps(meta or {"source_device_id": "test"}),
    )
    return str(path)


def _shift_model(tmp_path: Path, start: int = 7) -> str:
    """Deterministic chain: initial one-hot at *start*, row x -> (x+1) % 256."""
    initial = np.zeros(256)
    initial[start] = 1.0
    transition = np.zeros((256, 256))
    for x in range(256):
        transition[x, (x + 1) % 256] = 1.0
    return _write_model(tmp_path / "shift.npz", initial, transition)


def _markov_config(model: str, seed: str = SEED, **overrides) -> ControlConfig:
    kwargs = {"id": "prng-markov-0", "type": "prng_markov", "seed": seed, "model": model}
    kwargs.update(overrides)
    return ControlConfig(**kwargs)


class TestUniform:
    def test_chunking_independent_stream(self):
        a = PrngUniformControl(_uniform_config())
        b = PrngUniformControl(_uniform_config())
        assert a.read(7) + a.read(9) + a.read(16) == b.read(32)

    def test_different_seeds_differ(self):
        a = PrngUniformControl(_uniform_config())
        b = PrngUniformControl(_uniform_config(seed=SEED2))
        assert a.read(64) != b.read(64)

    def test_offset_regeneration(self):
        control = PrngUniformControl(_uniform_config())
        control.read(100)
        block = control.read(50)
        assert uniform_stream_bytes(int(SEED, 16), 100, 50) == block

    def test_stream_offset_is_monotonic(self):
        control = PrngUniformControl(_uniform_config())
        assert control.stream_offset_bytes == 0
        control.read(10)
        control.read(3)
        assert control.stream_offset_bytes == 13

    def test_unaligned_offsets(self):
        # Offsets that are not multiples of the 8-byte raw-word size.
        full = uniform_stream_bytes(int(SEED, 16), 0, 64)
        for offset, n in [(1, 5), (3, 8), (7, 17), (8, 8), (13, 40)]:
            assert uniform_stream_bytes(int(SEED, 16), offset, n) == full[offset : offset + n]

    def test_zero_length_read(self):
        assert PrngUniformControl(_uniform_config()).read(0) == b""

    def test_warns_not_quantum_at_construction(self, caplog):
        with caplog.at_level(logging.WARNING):
            PrngUniformControl(_uniform_config())
        assert "NOT quantum" in caplog.text


class TestMarkov:
    def test_deterministic_shift_chain(self, tmp_path):
        control = PrngMarkovControl(_markov_config(_shift_model(tmp_path, start=7)))
        # First byte from `initial`, then each byte from transition[prev].
        assert control.read(5) == bytes([7, 8, 9, 10, 11])
        # prev_byte persists across requests within a session.
        assert control.read(3) == bytes([12, 13, 14])

    def test_chunking_independent_stream(self, tmp_path):
        model = _write_model(tmp_path / "m.npz")
        a = PrngMarkovControl(_markov_config(model))
        b = PrngMarkovControl(_markov_config(model))
        assert a.read(10) + a.read(20) == b.read(30)

    def test_offset_regeneration_replays_chain(self, tmp_path):
        model = _write_model(tmp_path / "m.npz")
        control = PrngMarkovControl(_markov_config(model))
        control.read(10)
        block = control.read(20)
        assert markov_stream_bytes(model, int(SEED, 16), 10, 20) == block

    def test_model_sha256_and_meta_exposed(self, tmp_path):
        model = _write_model(tmp_path / "m.npz", meta={"source_device_id": "df-0"})
        control = PrngMarkovControl(_markov_config(model))
        assert len(control.model_sha256) == 64
        assert control.meta["source_device_id"] == "df-0"

    def test_missing_model_file_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            PrngMarkovControl(_markov_config(str(tmp_path / "nope.npz")))

    def test_non_stochastic_rows_rejected(self, tmp_path):
        transition = np.full((256, 256), 1 / 256)
        transition[3, 3] += 0.5  # row 3 sums to 1.5
        model = _write_model(tmp_path / "bad.npz", transition=transition)
        with pytest.raises(ConfigError, match="sum to 1"):
            load_markov_model(model)

    def test_bad_initial_sum_rejected(self, tmp_path):
        model = _write_model(tmp_path / "bad.npz", initial=np.full(256, 1 / 128))
        with pytest.raises(ConfigError, match="initial"):
            load_markov_model(model)

    def test_wrong_shapes_rejected(self, tmp_path):
        path = tmp_path / "bad.npz"
        np.savez(path, initial=np.full(16, 1 / 16), transition=np.full((256, 256), 1 / 256),
                 meta="{}")
        with pytest.raises(ConfigError, match="initial"):
            load_markov_model(str(path))

    def test_missing_arrays_rejected(self, tmp_path):
        path = tmp_path / "bad.npz"
        np.savez(path, initial=np.full(256, 1 / 256))
        with pytest.raises(ConfigError, match="missing"):
            load_markov_model(str(path))

    def test_warns_not_quantum_at_construction(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            PrngMarkovControl(_markov_config(_write_model(tmp_path / "m.npz")))
        assert "NOT quantum" in caplog.text


class TestFactory:
    def test_make_control_dispatch(self, tmp_path):
        assert isinstance(make_control(_uniform_config()), PrngUniformControl)
        model = _write_model(tmp_path / "m.npz")
        assert isinstance(make_control(_markov_config(model)), PrngMarkovControl)

    def test_kind_is_prng(self):
        assert make_control(_uniform_config()).kind == "prng"
