"""API-key store: lifecycle, hashing, bootstrap, usage accounting."""

import pytest_asyncio

from qbert0g.database import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "keys.db"))
    await database.connect(bootstrap_admin_key="bootstrap-admin-secret")
    yield database
    await database.disconnect()


class TestBootstrap:
    async def test_bootstrap_admin_created_and_valid(self, db):
        info = await db.validate_api_key("bootstrap-admin-secret")
        assert info is not None
        assert info["is_admin"] is True
        assert info["primary_device_id"] == "*"

    async def test_bootstrap_is_idempotent(self, db):
        await db._create_bootstrap_admin("bootstrap-admin-secret")
        keys = await db.list_api_keys()
        assert sum(1 for k in keys if k["name"] == "Bootstrap Admin") == 1


class TestKeyLifecycle:
    async def test_create_validate_roundtrip(self, db):
        raw, info = await db.create_api_key("client-a", "mock-0", rate_limit=5)
        validated = await db.validate_api_key(raw)
        assert validated["id"] == info["id"]
        assert validated["rate_limit"] == 5
        assert validated["primary_device_id"] == "mock-0"

    async def test_raw_key_never_stored(self, db):
        raw, _ = await db.create_api_key("client-b", "mock-0")
        cursor = await db.conn.execute("SELECT key_hash, key_prefix FROM api_keys")
        for row in await cursor.fetchall():
            assert row["key_hash"] != raw
        # only the 8-char prefix is retained for display
        assert (await db.list_api_keys())[0]["key_prefix"] == raw[:8]

    async def test_invalid_key_rejected(self, db):
        assert await db.validate_api_key("not-a-key") is None

    async def test_disabled_key_rejected(self, db):
        raw, info = await db.create_api_key("client-c", "mock-0")
        await db.update_api_key(info["id"], enabled=False)
        assert await db.validate_api_key(raw) is None
        await db.update_api_key(info["id"], enabled=True)
        assert await db.validate_api_key(raw) is not None

    async def test_delete(self, db):
        raw, info = await db.create_api_key("client-d", "mock-0")
        assert await db.delete_api_key(info["id"]) is True
        assert await db.validate_api_key(raw) is None
        assert await db.delete_api_key(info["id"]) is False


class TestUsage:
    async def test_usage_accumulates(self, db):
        _, info = await db.create_api_key("client-e", "mock-0")
        await db.record_usage(info["id"], 100)
        await db.record_usage(info["id"], 50)
        today = await db.get_usage_today(info["id"])
        assert today == {"requests": 2, "bytes_served": 150}

    async def test_usage_stats_shape(self, db):
        _, info = await db.create_api_key("client-f", "mock-0")
        await db.record_usage(info["id"], 64)
        stats = await db.get_usage_stats(info["id"])
        assert stats["total_bytes"] == 64
        assert stats["today_requests"] == 1
        assert stats["history"][0]["bytes_served"] == 64
