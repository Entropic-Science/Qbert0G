"""API-key storage and usage tracking (SQLite via aiosqlite).

Keys are stored as SHA-256 hashes; the raw key is printed exactly once
at creation. Per-key overrides (rate limit, daily byte cap, per-request
cap) fall back to the service-wide defaults in ``limits`` when NULL.

No global singleton: construct ``Database(path)`` and ``connect()`` it.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta

import aiosqlite


def _utcnow() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


class Database:
    """Async SQLite operations for API keys and usage records."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self, bootstrap_admin_key: str = "") -> None:
        """Open the database, create tables, and seed the bootstrap admin.

        Args:
            bootstrap_admin_key: When non-empty and not already present,
                an enabled admin key (device ``*``) is created for it.
                Changing the value later ADDS a new admin — revoke old
                keys with ``qbert0g keys disable/delete``.
        """
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()
        if bootstrap_admin_key:
            await self._create_bootstrap_admin(bootstrap_admin_key)

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() was not called"
        return self._conn

    async def _create_tables(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                name TEXT NOT NULL,
                primary_device_id TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                rate_limit INTEGER,
                daily_byte_limit INTEGER,
                max_bytes_per_request INTEGER,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

            CREATE TABLE IF NOT EXISTS usage_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id TEXT NOT NULL,
                date TEXT NOT NULL,
                requests INTEGER DEFAULT 0,
                bytes_served INTEGER DEFAULT 0,
                FOREIGN KEY (key_id) REFERENCES api_keys(id) ON DELETE CASCADE,
                UNIQUE(key_id, date)
            );

            CREATE INDEX IF NOT EXISTS idx_usage_key_date ON usage_records(key_id, date);
            """
        )
        await self.conn.commit()

    async def _create_bootstrap_admin(self, api_key: str) -> None:
        key_hash = self._hash_key(api_key)
        cursor = await self.conn.execute(
            "SELECT id FROM api_keys WHERE key_hash = ?", (key_hash,)
        )
        if await cursor.fetchone():
            return
        await self.conn.execute(
            """INSERT INTO api_keys
               (id, key_hash, key_prefix, name, primary_device_id, is_admin, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, 1, 1, ?)""",
            (str(uuid.uuid4()), key_hash, api_key[:8], "Bootstrap Admin", "*", _utcnow()),
        )
        await self.conn.commit()

    @staticmethod
    def _hash_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()

    @staticmethod
    def _generate_key() -> str:
        return secrets.token_urlsafe(32)

    async def create_api_key(
        self,
        name: str,
        primary_device_id: str,
        is_admin: bool = False,
        rate_limit: int | None = None,
        daily_byte_limit: int | None = None,
        max_bytes_per_request: int | None = None,
    ) -> tuple[str, dict]:
        """Create a key; returns ``(raw_key, info)``. Raw key shown once."""
        api_key = self._generate_key()
        key_id = str(uuid.uuid4())
        now = _utcnow()
        await self.conn.execute(
            """INSERT INTO api_keys
               (id, key_hash, key_prefix, name, primary_device_id, is_admin, enabled,
                rate_limit, daily_byte_limit, max_bytes_per_request, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (
                key_id,
                self._hash_key(api_key),
                api_key[:8],
                name,
                primary_device_id,
                1 if is_admin else 0,
                rate_limit,
                daily_byte_limit,
                max_bytes_per_request,
                now,
            ),
        )
        await self.conn.commit()
        return api_key, {
            "id": key_id,
            "name": name,
            "primary_device_id": primary_device_id,
            "is_admin": is_admin,
            "enabled": True,
            "rate_limit": rate_limit,
            "daily_byte_limit": daily_byte_limit,
            "max_bytes_per_request": max_bytes_per_request,
            "created_at": now,
        }

    async def validate_api_key(self, api_key: str) -> dict | None:
        """Return key info for a valid, enabled key; else ``None``."""
        cursor = await self.conn.execute(
            """SELECT id, name, primary_device_id, is_admin, enabled,
                      rate_limit, daily_byte_limit, max_bytes_per_request,
                      created_at
               FROM api_keys WHERE key_hash = ?""",
            (self._hash_key(api_key),),
        )
        row = await cursor.fetchone()
        if not row or not row["enabled"]:
            return None
        now = _utcnow()
        await self.conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, row["id"])
        )
        await self.conn.commit()
        return {
            "id": row["id"],
            "name": row["name"],
            "primary_device_id": row["primary_device_id"],
            "is_admin": bool(row["is_admin"]),
            "enabled": True,
            "rate_limit": row["rate_limit"],
            "daily_byte_limit": row["daily_byte_limit"],
            "max_bytes_per_request": row["max_bytes_per_request"],
            "created_at": row["created_at"],
            "last_used_at": now,
        }

    @staticmethod
    def _row_to_info(row: aiosqlite.Row) -> dict:
        return {
            "id": row["id"],
            "key_prefix": row["key_prefix"],
            "name": row["name"],
            "primary_device_id": row["primary_device_id"],
            "is_admin": bool(row["is_admin"]),
            "enabled": bool(row["enabled"]),
            "rate_limit": row["rate_limit"],
            "daily_byte_limit": row["daily_byte_limit"],
            "max_bytes_per_request": row["max_bytes_per_request"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }

    _KEY_COLUMNS = (
        "id, key_prefix, name, primary_device_id, is_admin, enabled, "
        "rate_limit, daily_byte_limit, max_bytes_per_request, created_at, last_used_at"
    )

    async def get_api_key_by_id(self, key_id: str) -> dict | None:
        cursor = await self.conn.execute(
            f"SELECT {self._KEY_COLUMNS} FROM api_keys WHERE id = ?", (key_id,)  # noqa: S608
        )
        row = await cursor.fetchone()
        return self._row_to_info(row) if row else None

    async def list_api_keys(self) -> list[dict]:
        cursor = await self.conn.execute(
            f"SELECT {self._KEY_COLUMNS} FROM api_keys ORDER BY created_at DESC"  # noqa: S608
        )
        return [self._row_to_info(row) for row in await cursor.fetchall()]

    async def update_api_key(
        self,
        key_id: str,
        name: str | None = None,
        primary_device_id: str | None = None,
        enabled: bool | None = None,
        rate_limit: int | None = None,
        daily_byte_limit: int | None = None,
        max_bytes_per_request: int | None = None,
    ) -> bool:
        """Update key settings. Returns True if the key existed."""
        updates: list[str] = []
        params: list = []
        for column, value in (
            ("name", name),
            ("primary_device_id", primary_device_id),
            ("enabled", None if enabled is None else int(enabled)),
            ("rate_limit", rate_limit),
            ("daily_byte_limit", daily_byte_limit),
            ("max_bytes_per_request", max_bytes_per_request),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)
        if not updates:
            return True
        params.append(key_id)
        cursor = await self.conn.execute(
            f"UPDATE api_keys SET {', '.join(updates)} WHERE id = ?", params  # noqa: S608
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def delete_api_key(self, key_id: str) -> bool:
        cursor = await self.conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        await self.conn.commit()
        return cursor.rowcount > 0

    async def record_usage(self, key_id: str, bytes_served: int) -> None:
        await self.conn.execute(
            """INSERT INTO usage_records (key_id, date, requests, bytes_served)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(key_id, date) DO UPDATE SET
                   requests = requests + 1,
                   bytes_served = bytes_served + ?""",
            (key_id, date.today().isoformat(), bytes_served, bytes_served),
        )
        await self.conn.commit()

    async def get_usage_today(self, key_id: str) -> dict:
        cursor = await self.conn.execute(
            "SELECT requests, bytes_served FROM usage_records WHERE key_id = ? AND date = ?",
            (key_id, date.today().isoformat()),
        )
        row = await cursor.fetchone()
        if not row:
            return {"requests": 0, "bytes_served": 0}
        return {"requests": row["requests"], "bytes_served": row["bytes_served"]}

    async def get_usage_stats(self, key_id: str, days: int = 7) -> dict | None:
        key_info = await self.get_api_key_by_id(key_id)
        if not key_info:
            return None
        start_date = (date.today() - timedelta(days=days)).isoformat()
        cursor = await self.conn.execute(
            """SELECT date, requests, bytes_served FROM usage_records
               WHERE key_id = ? AND date >= ? ORDER BY date DESC""",
            (key_id, start_date),
        )
        rows = await cursor.fetchall()
        today_usage = await self.get_usage_today(key_id)
        return {
            "key_id": key_id,
            "key_name": key_info["name"],
            "primary_device_id": key_info["primary_device_id"],
            "period_days": days,
            "total_requests": sum(r["requests"] for r in rows),
            "total_bytes": sum(r["bytes_served"] for r in rows),
            "today_requests": today_usage["requests"],
            "today_bytes": today_usage["bytes_served"],
            "max_bytes_per_request": key_info["max_bytes_per_request"],
            "daily_byte_limit": key_info["daily_byte_limit"],
            "history": [
                {"date": r["date"], "requests": r["requests"], "bytes_served": r["bytes_served"]}
                for r in rows
            ],
        }
