"""Shared request pipeline: authenticate, enforce limits, measure.

Both gRPC services (``qrng.QuantumRNG`` and ``qr_entropy.EntropyService``)
funnel every request through :meth:`RequestGate.measure`, so auth, rate
limiting, byte caps, device routing and usage accounting behave
identically regardless of which protocol a client speaks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import grpc

from .config import Config
from .database import Database
from .devices import DeviceManager

logger = logging.getLogger(__name__)


class RateLimiter:
    """In-memory sliding-window rate limiter (per key, per minute)."""

    def __init__(self) -> None:
        self._request_times: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def allow(self, key_id: str, limit: int) -> bool:
        async with self._lock:
            now = time.time()
            window = [ts for ts in self._request_times[key_id] if ts > now - 60]
            if len(window) >= limit:
                self._request_times[key_id] = window
                return False
            window.append(now)
            self._request_times[key_id] = window
            return True


@dataclass(frozen=True)
class Measurement:
    """One completed entropy measurement."""

    data: bytes
    device_id: str
    timestamp_ns: int  # measurement time (epoch ns); 0 when emission is off


class RequestGate:
    """Auth + limits + device read, shared by all gRPC services."""

    def __init__(self, config: Config, db: Database, devices: DeviceManager) -> None:
        self._config = config
        self._db = db
        self._devices = devices
        self._rate_limiter = RateLimiter()

    async def measure(self, context: grpc.aio.ServicerContext, num_bytes: int) -> Measurement:
        """Validate the request end-to-end and read fresh bytes.

        Aborts the RPC (raising) with the appropriate status code on any
        failure; on success records usage and returns a
        :class:`Measurement` stamped at read-completion time.
        """
        config = self._config

        # ── auth ────────────────────────────────────────────────────────
        metadata = dict(context.invocation_metadata())
        api_key = metadata.get(config.auth.header, "")
        if not api_key:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                f"API key required (pass via metadata key {config.auth.header!r})",
            )
        key_info = await self._db.validate_api_key(api_key)
        if not key_info:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid API key")

        # ── rate limit ──────────────────────────────────────────────────
        rate_limit = key_info.get("rate_limit")
        if rate_limit is None:
            rate_limit = config.limits.rate_limit_per_minute
        if not await self._rate_limiter.allow(key_info["id"], rate_limit):
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Rate limit exceeded. Limit: {rate_limit} requests per minute.",
            )

        # ── byte caps ───────────────────────────────────────────────────
        if num_bytes < 1:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "requested byte count must be at least 1"
            )
        max_per_request = key_info.get("max_bytes_per_request")
        if max_per_request is None:
            max_per_request = config.limits.max_bytes_per_request
        if num_bytes > max_per_request:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"Request for {num_bytes} bytes exceeds the limit of "
                f"{max_per_request} bytes per request",
            )
        daily_limit = key_info.get("daily_byte_limit")
        if daily_limit is None:
            daily_limit = config.limits.max_bytes_per_day
        today_usage = await self._db.get_usage_today(key_info["id"])
        if today_usage["bytes_served"] + num_bytes > daily_limit:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Daily byte limit exceeded. Used: {today_usage['bytes_served']}, "
                f"Limit: {daily_limit}",
            )

        # ── device routing ──────────────────────────────────────────────
        primary_device = key_info["primary_device_id"]
        if primary_device == "*":
            if self._devices.devices:
                primary_device = next(iter(self._devices.devices))
            else:
                await context.abort(grpc.StatusCode.UNAVAILABLE, "No devices available")

        try:
            data, serving_device = await self._devices.read_bytes(
                primary_device, num_bytes, timeout=config.server.request_timeout
            )
        except TimeoutError:
            await context.abort(
                grpc.StatusCode.DEADLINE_EXCEEDED, "Request timed out waiting for device"
            )
        except Exception as exc:
            logger.error("Device error: %s", exc)
            await context.abort(grpc.StatusCode.UNAVAILABLE, f"Device error: {exc}")

        timestamp_ns = time.time_ns() if self._config.freshness.emit_generation_timestamp else 0

        await self._db.record_usage(key_info["id"], num_bytes)
        return Measurement(data=bytes(data), device_id=serving_device, timestamp_ns=timestamp_ns)
