"""Device manager behavior via the mock driver (no hardware)."""

import pytest_asyncio
from conftest import make_config

from qbert0g.devices import DeviceManager, DeviceStatus


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
