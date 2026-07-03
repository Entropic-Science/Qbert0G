"""SourceRouter: one id namespace, profile no-failover, skew, provenance."""

import json
import logging
import time

import pytest
import pytest_asyncio
from conftest import SEED_A, SEED_B, make_profile_config

import qbert0g.devices as devices_mod
from qbert0g.controls import uniform_stream_bytes
from qbert0g.devices import DeviceManager, DeviceStatus
from qbert0g.profiles import parity_bytes_needed, xnor
from qbert0g.sources import (
    ProvenanceLog,
    ProvenanceWriteError,
    SourceRouter,
    SourceUnavailableError,
)


@pytest_asyncio.fixture
async def router(tmp_path):
    config = make_profile_config(tmp_path)
    manager = DeviceManager(config)
    await manager.initialize()
    r = SourceRouter(config, manager)
    yield r
    await manager.shutdown()


class TestResolution:
    async def test_plain_device_delegates_unchanged(self, router):
        read = await router.read("mock-0", 64)
        assert len(read.data) == 64
        assert read.source_id == "mock-0"
        assert read.transform is None
        assert read.inputs == [{"id": "mock-0", "kind": "mock", "raw_bytes": 64}]
        assert read.max_pair_skew_ns is None

    async def test_plain_device_still_fails_over(self, router):
        router._devices.devices["mock-0"].status = DeviceStatus.ERROR
        read = await router.read("mock-0", 8)
        assert read.source_id == "mock-1"  # failover intact for plain devices

    async def test_profile_id_resolves(self, router):
        read = await router.read("qq-mock", 128)
        assert len(read.data) == 128
        assert read.source_id == "qq-mock"
        assert read.transform == "xnor"
        assert [f["id"] for f in read.inputs] == ["mock-0", "mock-1"]
        assert all(f["kind"] == "mock" for f in read.inputs)
        assert read.max_pair_skew_ns is not None and read.max_pair_skew_ns >= 0

    async def test_profile_never_fails_over(self, router):
        # mock-1 is healthy, but qq-mock's composition includes mock-0:
        # the arm must fail, never silently recompose.
        router._devices.devices["mock-0"].status = DeviceStatus.ERROR
        with pytest.raises(SourceUnavailableError, match="never fail over"):
            await router.read("qq-mock", 16)

    async def test_default_device_id_skips_controls_and_profiles(self, router):
        assert router.default_device_id() == "mock-0"


class TestControls:
    async def test_control_read_is_reproducible_from_fact(self, router):
        read1 = await router.read("prng-a", 100)
        read2 = await router.read("prng-a", 50)
        fact1, fact2 = read1.inputs[0], read2.inputs[0]
        assert fact1["kind"] == "prng"
        assert (fact1["seed"], fact1["stream_offset_bytes"]) == (SEED_A, 0)
        assert fact2["stream_offset_bytes"] == 100  # contiguous stream
        # Offline regeneration from nothing but the provenance facts:
        assert read1.data == uniform_stream_bytes(int(SEED_A, 16), 0, 100)
        assert read2.data == uniform_stream_bytes(int(SEED_A, 16), 100, 50)

    async def test_pure_prng_profile_is_deterministic(self, router):
        read = await router.read("pp-match", 256)
        expected = xnor(
            uniform_stream_bytes(int(SEED_A, 16), 0, 256),
            uniform_stream_bytes(int(SEED_B, 16), 0, 256),
        )
        assert read.data == expected
        assert [f["kind"] for f in read.inputs] == ["prng", "prng"]
        assert read.max_pair_skew_ns is None  # no pairing constraints for PRNG


class TestMixedAndParity:
    @pytest_asyncio.fixture
    async def mixed_router(self, tmp_path):
        config = make_profile_config(
            tmp_path,
            profiles=[
                {"id": "qp-mix", "transform": "xnor", "inputs": ["mock-0", "prng-a"]},
                {
                    "id": "par-prng",
                    "transform": "parity",
                    "inputs": ["prng-a"],
                    "params": {"taps": [0, 9, 19, 30], "stride": 4, "allow_period4": True},
                },
            ],
        )
        manager = DeviceManager(config)
        await manager.initialize()
        yield SourceRouter(config, manager)
        await manager.shutdown()

    async def test_mixed_profile_kinds_are_explicit(self, mixed_router):
        read = await mixed_router.read("qp-mix", 64)
        assert len(read.data) == 64
        kinds = {f["id"]: f["kind"] for f in read.inputs}
        assert kinds == {"mock-0": "mock", "prng-a": "prng"}
        assert read.inputs[1]["seed"] == SEED_A  # prng regenerability facts present

    async def test_parity_raw_consumption_recorded(self, mixed_router):
        read = await mixed_router.read("par-prng", 32)
        assert len(read.data) == 32
        need = parity_bytes_needed(32, [0, 9, 19, 30], 4)
        assert read.inputs[0]["raw_bytes"] == need  # raw consumption is a provenance fact


class TestPairedSkew:
    async def test_skew_over_threshold_warns_but_serves(self, tmp_path, caplog):
        config = make_profile_config(tmp_path, profiles_defaults={"max_skew_ns": 1})
        manager = DeviceManager(config)
        await manager.initialize()
        try:
            # Delay one device so every chunk pair has measurable skew.
            slow = manager.devices["mock-1"].driver
            real = slow.start_one_shot
            slow.start_one_shot = lambda n: (time.sleep(0.002), real(n))[1]
            router = SourceRouter(config, manager)
            with caplog.at_level(logging.WARNING, logger="qbert0g.sources"):
                read = await router.read("qq-mock", 64)
            assert len(read.data) == 64  # served anyway
            assert read.max_pair_skew_ns > 1
            warnings = [r for r in caplog.records if "pair skew" in r.getMessage()]
            assert len(warnings) == 1
        finally:
            await manager.shutdown()

    async def test_paired_read_chunks_and_stamps(self, tmp_path):
        config = make_profile_config(tmp_path, profiles_defaults={"chunk_bytes": 16})
        manager = DeviceManager(config)
        await manager.initialize()
        try:
            router = SourceRouter(config, manager)
            read = await router.read("qq-mock", 64)  # 4 chunk pairs
            assert len(read.data) == 64
            for fact in read.inputs:
                assert fact["last_chunk_ns"] >= fact["first_chunk_ns"]
        finally:
            await manager.shutdown()


# ── chardev inputs: freshness once per request + health provenance ──────

PCI_0 = "0000_09_00.0"
PCI_1 = "0000_0a_00.0"
PATTERN_A = bytes(range(256)) * 16
PATTERN_B = bytes(reversed(range(256))) * 16


def _chardev_entry(tmp_path, name, pci, payload, *, ready_count="0", error_bits="0000000"):
    node = tmp_path / name
    node.write_bytes(payload)
    sysfs_dir = tmp_path / "sys" / pci
    sysfs_dir.mkdir(parents=True)
    (sysfs_dir / "ready_count").write_text(ready_count)
    (sysfs_dir / "error_present").write_text("1" if error_bits.strip("0") else "0")
    (sysfs_dir / "error_bits").write_text(error_bits)
    return {"id": name, "type": "chardev", "path": str(node), "pci_address": pci}


class TestChardevProfiles:
    async def test_paired_flush_once_then_health_in_facts(self, tmp_path, monkeypatch):
        # df-a has 8 stale bytes (ready_count=2 words). If the flush ran
        # per CHUNK instead of per REQUEST, each chunk would drain 8 more
        # bytes and the served stream could not be the contiguous
        # PATTERN_A[8:] — byte equality below proves flush-once.
        monkeypatch.setattr(devices_mod, "_SYSFS_PCI_ROOT", str(tmp_path / "sys"))
        dev_a = _chardev_entry(
            tmp_path, "df-a", PCI_0, b"S" * 8 + PATTERN_A, ready_count="2",
            error_bits="1100000",
        )
        dev_b = _chardev_entry(tmp_path, "df-b", PCI_1, PATTERN_B)
        config = make_profile_config(
            tmp_path,
            devices=[dev_a, dev_b],
            profiles=[{"id": "qq-df", "transform": "xnor", "inputs": ["df-a", "df-b"]}],
            profiles_defaults={"chunk_bytes": 16},
        )
        manager = DeviceManager(config)
        await manager.initialize()
        try:
            router = SourceRouter(config, manager)
            read = await router.read("qq-df", 64)
            assert read.data == xnor(PATTERN_A[:64], PATTERN_B[:64])
            facts = {f["id"]: f for f in read.inputs}
            assert facts["df-a"]["kind"] == "quantum"
            assert facts["df-a"]["flushed_bytes"] == 8
            assert facts["df-a"]["error_present"] == "1"
            assert facts["df-a"]["error_bits"] == "1100000"
            assert facts["df-b"]["flushed_bytes"] == 0
        finally:
            await manager.shutdown()


# ── provenance log ───────────────────────────────────────────────────────


class TestProvenance:
    async def test_record_shape(self, router, tmp_path):
        read = await router.read("qq-mock", 32)
        record = router.record_provenance(
            read, protocol="qr_entropy", served_bytes=32, sequence_id=123, api_key_id="key-1"
        )
        lines = (tmp_path / "provenance.jsonl").read_text().splitlines()
        assert json.loads(lines[-1]) == record
        assert record["source_id"] == "qq-mock"
        assert record["protocol"] == "qr_entropy"
        assert record["sequence_id"] == 123
        assert record["served_bytes"] == 32
        assert record["transform"] == "xnor"
        assert record["max_pair_skew_ns"] >= 0
        assert record["api_key_id"] == "key-1"
        assert {f["kind"] for f in record["inputs"]} == {"mock"}
        assert record["ts"] and record["request_id"]

    def test_write_failure_logs_and_continues(self, tmp_path, caplog):
        log = ProvenanceLog(str(tmp_path), strict=False)  # a directory: open() fails
        with caplog.at_level(logging.ERROR, logger="qbert0g.sources"):
            log.write({"x": 1})  # must NOT raise
        assert any("Provenance write" in r.getMessage() for r in caplog.records)

    def test_strict_write_failure_raises(self, tmp_path):
        log = ProvenanceLog(str(tmp_path), strict=True)
        with pytest.raises(ProvenanceWriteError):
            log.write({"x": 1})


class TestDescribe:
    async def test_rows_cover_all_kinds(self, router):
        rows = {r["id"]: r for r in router.describe()}
        assert rows["mock-0"]["kind"] == "mock"
        assert rows["prng-a"]["kind"] == "prng"
        assert rows["prng-a"]["availability"] == "ready"
        assert rows["qq-mock"]["kind"] == "profile"
        assert rows["qq-mock"]["transform"] == "xnor"
        assert rows["qq-mock"]["inputs"] == "mock-0,mock-1"
        assert rows["qq-mock"]["availability"] == "ready"

    async def test_profile_with_downed_input_shows_unavailable(self, router):
        router._devices.devices["mock-1"].status = DeviceStatus.ERROR
        rows = {r["id"]: r for r in router.describe()}
        assert "unavailable" in rows["qq-mock"]["availability"]
        assert "mock-1" in rows["qq-mock"]["availability"]
