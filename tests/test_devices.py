"""Device manager behavior via the mock driver (no hardware)."""

import logging
import os

import pytest
import pytest_asyncio
from conftest import make_config

import qbert0g.devices as devices_mod
from qbert0g.devices import ChardevDevice, DeviceManager, DeviceStatus


@pytest_asyncio.fixture
async def manager(tmp_path):
    config = make_config(
        tmp_path,
        devices=[
            {"id": "mock-0", "type": "mock"},
            {"id": "mock-1", "type": "mock", "streaming_mode": True},
        ],
    )
    mgr = DeviceManager(config)
    await mgr.initialize()
    yield mgr
    await mgr.shutdown()


class TestReads:
    async def test_oneshot_read(self, manager):
        data, device_id = await manager.read_bytes("mock-0", 100)
        assert len(data) == 100
        assert device_id == "mock-0"

    async def test_streaming_read(self, manager):
        data, device_id = await manager.read_bytes("mock-1", 100)
        assert len(data) == 100
        assert device_id == "mock-1"
        assert manager.devices["mock-1"].streaming_active is True

    async def test_stats_accumulate(self, manager):
        await manager.read_bytes("mock-0", 10)
        await manager.read_bytes("mock-0", 20)
        status = manager.get_device_status("mock-0")
        assert status["requests_served"] == 2
        assert status["bytes_served"] == 30


class TestFailover:
    async def test_fallback_order_prefers_same_type(self, manager):
        assert manager.get_fallback_order("mock-1") == ["mock-1", "mock-0"]

    async def test_failover_to_healthy_device(self, manager):
        manager.devices["mock-0"].status = DeviceStatus.ERROR
        data, device_id = await manager.read_bytes("mock-0", 8)
        assert device_id == "mock-1"
        assert len(data) == 8

    async def test_no_failover_when_disabled(self, tmp_path):
        config = make_config(
            tmp_path,
            server={"listen": "127.0.0.1:0", "failover_enabled": False, "request_timeout": 0.2},
            devices=[
                {"id": "mock-0", "type": "mock"},
                {"id": "mock-1", "type": "mock"},
            ],
        )
        mgr = DeviceManager(config)
        await mgr.initialize()
        try:
            mgr.devices["mock-0"].status = DeviceStatus.ERROR
            import pytest

            with pytest.raises(TimeoutError):
                await mgr.read_bytes("mock-0", 8, timeout=0.2)
        finally:
            await mgr.shutdown()


class TestStatus:
    async def test_status_shape(self, manager):
        status = manager.get_device_status("mock-0")
        assert status["type"] == "mock"
        assert status["status"] == "online"
        assert status["post_processing"] == "raw"
        assert status["is_available"] is True


# ── chardev (PCIe Dragonfly as a character device) ──────────────────────
# No hardware in CI: the device node is a tmp file, sysfs is a tmp tree
# pointed at via the injectable module-level _SYSFS_PCI_ROOT.

# Stand-in PCI address: real ones ("0000:09:00.0") contain colons, which
# are illegal in Windows tmp-dir names — the manager treats it as opaque.
PCI = "0000_09_00.0"
#: Recognizable non-repeating payload; index == byte offset (mod 256).
PATTERN = bytes(range(256)) * 8


async def _chardev_manager(
    tmp_path,
    monkeypatch,
    *,
    node_bytes: bytes,
    pci: bool = True,
    ready_count: str = "0",
    error_present: str = "0",
    error_bits: str = "0000000",
    **config_overrides,
) -> DeviceManager:
    node = tmp_path / "qrngDF0"
    node.write_bytes(node_bytes)
    sysfs_root = tmp_path / "sys"
    sysfs_dir = sysfs_root / PCI
    sysfs_dir.mkdir(parents=True)
    (sysfs_dir / "ready_count").write_text(ready_count)
    (sysfs_dir / "error_present").write_text(error_present)
    (sysfs_dir / "error_bits").write_text(error_bits)
    monkeypatch.setattr(devices_mod, "_SYSFS_PCI_ROOT", str(sysfs_root))

    device: dict = {"id": "df-0", "type": "chardev", "path": str(node)}
    if pci:
        device["pci_address"] = PCI
    config = make_config(tmp_path, devices=[device], **config_overrides)
    mgr = DeviceManager(config)
    await mgr.initialize()
    return mgr


class TestChardev:
    async def test_exact_read(self, tmp_path, monkeypatch):
        mgr = await _chardev_manager(tmp_path, monkeypatch, node_bytes=PATTERN)
        try:
            data, device_id = await mgr.read_bytes("df-0", 100)
            assert device_id == "df-0"
            assert data == PATTERN[:100]  # exactly n bytes, from offset 0
            assert mgr.devices["df-0"].last_flushed_bytes == 0
        finally:
            await mgr.shutdown()

    async def test_freshness_drain_before_measurement_read(self, tmp_path, monkeypatch):
        # ready_count = 2 words -> 8 stale bytes drained BEFORE the read:
        # the measurement must start at offset 8 of the device stream.
        stale = b"S" * 8
        mgr = await _chardev_manager(
            tmp_path, monkeypatch, node_bytes=stale + PATTERN, ready_count="2"
        )
        try:
            data, _ = await mgr.read_bytes("df-0", 16)
            assert data == PATTERN[:16]
            assert mgr.devices["df-0"].last_flushed_bytes == 8
        finally:
            await mgr.shutdown()

    async def test_no_drain_when_flush_disabled_but_health_still_read(
        self, tmp_path, monkeypatch
    ):
        mgr = await _chardev_manager(
            tmp_path,
            monkeypatch,
            node_bytes=PATTERN,
            ready_count="2",
            error_bits="0000001",
            freshness={"flush_device_buffer": False},
        )
        try:
            data, _ = await mgr.read_bytes("df-0", 8)
            assert data == PATTERN[:8]  # nothing drained
            state = mgr.devices["df-0"]
            assert state.last_flushed_bytes is None
            assert state.last_error_bits == "0000001"  # snapshot taken regardless
        finally:
            await mgr.shutdown()

    async def test_one_time_warning_without_pci_address(self, tmp_path, monkeypatch, caplog):
        mgr = await _chardev_manager(tmp_path, monkeypatch, node_bytes=PATTERN, pci=False)
        try:
            with caplog.at_level(logging.WARNING, logger="qbert0g.devices"):
                await mgr.read_bytes("df-0", 8)
                await mgr.read_bytes("df-0", 8)
            warnings = [r for r in caplog.records if "freshness flush" in r.getMessage()]
            assert len(warnings) == 1  # emitted once, not per measurement
        finally:
            await mgr.shutdown()

    async def test_health_snapshot_exposed_in_status(self, tmp_path, monkeypatch):
        mgr = await _chardev_manager(
            tmp_path,
            monkeypatch,
            node_bytes=PATTERN,
            error_present="1",
            error_bits="1100000",
        )
        try:
            await mgr.read_bytes("df-0", 8)
            status = mgr.get_device_status("df-0")
            assert status["pci_address"] == PCI
            assert status["error_present"] == "1"
            assert status["error_bits"] == "1100000"
            assert status["last_flushed_bytes"] == 0
            assert status["post_processing"] is None  # not applicable to chardev
        finally:
            await mgr.shutdown()

    def test_short_read_looping(self, tmp_path, monkeypatch):
        # Force short reads: os.read returns at most 4 bytes per call.
        node = tmp_path / "qrngDF0"
        node.write_bytes(PATTERN)
        real_read = os.read
        monkeypatch.setattr(os, "read", lambda fd, n: real_read(fd, min(n, 4)))
        dev = ChardevDevice(str(node))
        try:
            assert dev.start_one_shot(10) == PATTERN[:10]
        finally:
            dev.close_comm()

    def test_eof_raises(self, tmp_path):
        node = tmp_path / "qrngDF0"
        node.write_bytes(b"1234")
        dev = ChardevDevice(str(node))
        try:
            with pytest.raises(RuntimeError, match="EOF"):
                dev.start_one_shot(10)
        finally:
            dev.close_comm()
