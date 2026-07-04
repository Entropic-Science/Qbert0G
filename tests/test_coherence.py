"""Coherence channel: block-correlation math + the background monitor."""

import asyncio
import json
import time

import numpy as np
import pytest
import pytest_asyncio
from conftest import make_profile_config

from qbert0g.coherence import (
    CoherenceInvalidError,
    CoherenceMonitor,
    CoherenceValue,
    block_correlation,
)
from qbert0g.config import CoherenceConfig
from qbert0g.devices import DeviceManager, DeviceStatus
from qbert0g.sources import SourceRouter

BLOCK = 64  # bytes per block for the math tests — small and fast


def blocks_from_fractions(fractions, rng, block_bytes=BLOCK) -> bytes:
    """Bytes whose per-block ones-fractions track *fractions* (plus noise)."""
    n_bits = block_bytes * 8
    out = bytearray()
    for p in fractions:
        bits = (rng.random(n_bits) < p).astype(np.uint8)
        out += np.packbits(bits).tobytes()
    return bytes(out)


class TestBlockCorrelation:
    def test_injected_correlation_at_lag_plus_two_recovered(self):
        rng = np.random.default_rng(7)
        # A shared signal drives A's blocks; B carries the SAME signal two
        # blocks later (B trails A), so the winning lag must be +2.
        wave = 0.18 * np.sin(np.linspace(0, 12 * np.pi, 64))
        signal = np.clip(0.5 + wave + 0.02 * rng.standard_normal(64), 0.2, 0.8)
        a = blocks_from_fractions(signal, rng)
        b = blocks_from_fractions(np.concatenate([[0.5, 0.5], signal[:-2]]), rng)
        r, lag, z_c, k_eff = block_correlation(
            a, b, block_bytes=BLOCK, lag_scan=4, min_valid_blocks=16
        )
        assert lag == 2
        assert r > 0.8
        assert z_c > 5.0  # a real injected correlation is loudly significant
        assert k_eff == 64 - 2

    def test_prng_pair_null_z_is_loosely_standard_normal(self):
        # 200 evaluations over INDEPENDENT streams. z_c is the max-|r| pick
        # over 9 lags, so it is wider-tailed than N(0,1) — the bounds are
        # deliberately loose; this is a sanity pin, not a calibration.
        rng = np.random.default_rng(42)
        n_blocks, z_values = 32, []
        for _ in range(200):
            a = rng.integers(0, 256, size=n_blocks * BLOCK, dtype=np.uint8).tobytes()
            b = rng.integers(0, 256, size=n_blocks * BLOCK, dtype=np.uint8).tobytes()
            _r, _lag, z_c, k_eff = block_correlation(
                a, b, block_bytes=BLOCK, lag_scan=4, min_valid_blocks=24
            )
            assert k_eff >= 24
            z_values.append(z_c)
        z = np.array(z_values)
        assert abs(float(z.mean())) < 0.75
        assert 0.3 < float(z.std()) < 3.0
        assert float(np.abs(z).max()) < 6.5

    def test_min_valid_blocks_refusal(self):
        rng = np.random.default_rng(0)
        data = rng.integers(0, 256, size=10 * BLOCK, dtype=np.uint8).tobytes()
        with pytest.raises(CoherenceInvalidError, match="min_valid_blocks=24"):
            block_correlation(data, data, block_bytes=BLOCK, lag_scan=2, min_valid_blocks=24)

    def test_hard_floor_of_four_blocks(self):
        # Fisher's sqrt(k - 3) degenerates below 4 blocks — even
        # min_valid_blocks=1 cannot lower the floor.
        rng = np.random.default_rng(1)
        data = rng.integers(0, 256, size=3 * BLOCK, dtype=np.uint8).tobytes()
        with pytest.raises(CoherenceInvalidError, match="at least 4"):
            block_correlation(data, data, block_bytes=BLOCK, lag_scan=0, min_valid_blocks=1)

    def test_block_bytes_below_one_is_rejected(self):
        with pytest.raises(ValueError, match="block_bytes must be >= 1"):
            block_correlation(
                b"\x00" * 64, b"\x00" * 64, block_bytes=0, lag_scan=2, min_valid_blocks=4
            )

    def test_negative_lag_scan_is_rejected(self):
        data = b"\x0f" * (8 * BLOCK)
        with pytest.raises(ValueError, match="lag_scan must be >= 0"):
            block_correlation(data, data, block_bytes=BLOCK, lag_scan=-1, min_valid_blocks=4)

    def test_zero_variance_series_is_invalid_not_fake(self):
        flat = b"\xff" * (32 * BLOCK)
        with pytest.raises(CoherenceInvalidError, match="zero-variance"):
            block_correlation(flat, flat, block_bytes=BLOCK, lag_scan=2, min_valid_blocks=8)

    def test_identical_streams_win_at_lag_zero_with_clipped_z(self):
        rng = np.random.default_rng(3)
        data = rng.integers(0, 256, size=32 * BLOCK, dtype=np.uint8).tobytes()
        r, lag, z_c, k_eff = block_correlation(
            data, data, block_bytes=BLOCK, lag_scan=4, min_valid_blocks=8
        )
        assert (r, lag, k_eff) == (1.0, 0, 32)
        assert np.isfinite(z_c) and z_c > 0  # atanh clip: large but finite


def make_coherence_config(**overrides) -> CoherenceConfig:
    defaults = dict(
        enabled=True,
        pair=["mock-0", "mock-1"],
        block_bytes=64,
        blocks_per_side=32,
        lag_scan_blocks=2,
        min_valid_blocks=8,
        refresh_s=0.01,
        max_age_s=5.0,
    )
    defaults.update(overrides)
    return CoherenceConfig(**defaults)


class TestSnapshotStaleness:
    def _value(self, computed_monotonic_ns: int) -> CoherenceValue:
        return CoherenceValue(
            r=0.1, lag=0, z_c=0.5, k_eff=32, max_pair_skew_ns=100,
            computed_monotonic_ns=computed_monotonic_ns,
        )

    def test_never_computed_is_invalid(self):
        monitor = CoherenceMonitor(make_coherence_config(), router=None)
        assert monitor.snapshot() == (None, False)

    def test_fresh_value_is_valid(self):
        monitor = CoherenceMonitor(make_coherence_config(), router=None)
        monitor._latest = self._value(time.monotonic_ns())
        value, valid = monitor.snapshot()
        assert valid and value.r == 0.1

    def test_stale_value_is_served_but_invalid(self):
        monitor = CoherenceMonitor(make_coherence_config(max_age_s=0.5), router=None)
        stale = self._value(time.monotonic_ns() - int(2e9))  # 2 s old
        monitor._latest = stale
        value, valid = monitor.snapshot()
        assert value is stale  # the value is still visible...
        assert valid is False  # ...but never claimed fresh


@pytest_asyncio.fixture
async def coherence_router(tmp_path):
    config = make_profile_config(
        tmp_path,
        coherence={
            "enabled": True,
            "pair": ["mock-0", "mock-1"],
            "block_bytes": 64,
            "blocks_per_side": 32,
            "lag_scan_blocks": 2,
            "min_valid_blocks": 8,
            "refresh_s": 0.01,
            "max_age_s": 5.0,
        },
    )
    manager = DeviceManager(config)
    await manager.initialize()
    yield config, SourceRouter(config, manager)
    await manager.shutdown()


def read_provenance(config) -> list[dict]:
    with open(config.provenance.path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class TestCoherenceMonitor:
    async def test_evaluate_once_publishes_value_and_provenance(self, coherence_router):
        config, router = coherence_router
        monitor = CoherenceMonitor(config.coherence, router)
        value = await monitor.evaluate_once()
        assert isinstance(value, CoherenceValue)
        assert monitor.latest is value
        snap, valid = monitor.snapshot()
        assert snap is value and valid
        assert -1.0 <= value.r <= 1.0
        assert -2 <= value.lag <= 2
        assert value.k_eff >= 8
        assert value.max_pair_skew_ns >= 0

        records = read_provenance(config)
        assert len(records) == 1
        rec = records[0]
        assert rec["protocol"] == "coherence"
        assert rec["source_id"] == "coherence(mock-0,mock-1)"
        assert rec["served_bytes"] == 0
        assert rec["api_key_id"] is None
        assert (rec["r"], rec["lag"], rec["z_c"]) == (value.r, value.lag, value.z_c)
        assert rec["max_pair_skew_ns"] == value.max_pair_skew_ns
        assert [i["kind"] for i in rec["inputs"]] == ["mock", "mock"]
        assert all(i["raw_bytes"] == 32 * 64 for i in rec["inputs"])

    async def test_failure_leaves_last_value_as_is(self, coherence_router):
        config, router = coherence_router
        monitor = CoherenceMonitor(config.coherence, router)
        first = await monitor.evaluate_once()
        assert first is not None
        n_records = len(read_provenance(config))

        router._devices.devices["mock-0"].status = DeviceStatus.ERROR
        result = await monitor.evaluate_once()  # must not raise
        assert result is first
        assert monitor.latest is first
        assert len(read_provenance(config)) == n_records  # failed cycle: no record

    async def test_monitor_lifecycle_start_and_cancel(self, coherence_router):
        config, router = coherence_router
        monitor = CoherenceMonitor(config.coherence, router)
        monitor.start()
        task = monitor._task
        monitor.start()  # idempotent: no second task
        assert monitor._task is task

        deadline = time.monotonic() + 5.0
        while monitor.latest is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert monitor.latest is not None

        await monitor.stop()
        assert monitor._task is None
        assert task.cancelled()
        await monitor.stop()  # idempotent after cancel
