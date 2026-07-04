"""Shared request pipeline: authenticate, enforce limits, measure.

Both gRPC services (``qrng.QuantumRNG`` and ``qr_entropy.EntropyService``)
funnel every request through :meth:`RequestGate.measure`, so auth, rate
limiting, byte caps, source routing (devices, PRNG controls, profiles —
via the :class:`~qbert0g.sources.SourceRouter`), provenance logging and
usage accounting behave identically regardless of which protocol a
client speaks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import grpc

from .config import Config
from .database import Database
from .sources import ProvenanceWriteError, SourceRead, SourceRouter, SourceUnavailableError

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
    device_id: str  # the SERVING source id: device, control, or profile
    timestamp_ns: int  # last contributing measurement (epoch ns); 0 when emission is off


class RequestGate:
    """Auth + limits + source read, shared by all gRPC services.

    The read step goes through the :class:`SourceRouter`, so API keys
    bind to ANY source id — device, PRNG control, or profile — with one
    unchanged validation pipeline. Byte caps, rate limits, and usage
    accounting apply to SERVED bytes; raw input consumption of profiles
    is a provenance fact, not a limits fact.
    """

    def __init__(self, config: Config, db: Database, router: SourceRouter) -> None:
        self._config = config
        self._db = db
        self._router = router
        self._rate_limiter = RateLimiter()

    async def measure(
        self,
        context: grpc.aio.ServicerContext,
        num_bytes: int,
        *,
        protocol: str,
        sequence_id: int | None = None,
        requested_source_id: str | None = None,
        provenance_extras: dict | Callable[[SourceRead], Awaitable[dict]] | None = None,
    ) -> Measurement:
        """Validate the request end-to-end and read fresh bytes.

        Aborts the RPC (raising) with the appropriate status code on any
        failure; on success writes the provenance record, records usage,
        and returns a :class:`Measurement`. *protocol* / *sequence_id*
        exist solely for the provenance record.

        *requested_source_id* overrides the API key's source binding —
        honored ONLY for ``protocol="qr_purity"`` (the PurityService
        draw path) and only for ids in ``integration.sources``; any
        other combination aborts ``INVALID_ARGUMENT``. Defaults preserve
        the existing behavior exactly.

        *provenance_extras* is merged flat into the (single) provenance
        record. It may be a plain dict, or an async callable receiving
        the completed :class:`~qbert0g.sources.SourceRead` — the draw
        path needs extras (z, purity label, ...) that depend on the
        measured bytes, and "exactly one provenance record per request"
        forbids a second write. The callable runs after the read and
        before the record is written.
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

        # ── source routing ──────────────────────────────────────────────
        if requested_source_id is not None:
            if protocol != "qr_purity":
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "per-request source selection is a PurityService draw feature",
                )
            if requested_source_id not in config.integration.sources:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"source {requested_source_id!r} is not drawable "
                    "(not in integration.sources)",
                )
            primary_source = requested_source_id
        else:
            primary_source = key_info["primary_device_id"]  # binds to ANY source id
            if primary_source == "*":
                primary_source = self._router.default_device_id()
                if primary_source is None:
                    await context.abort(grpc.StatusCode.UNAVAILABLE, "No devices available")

        try:
            read = await self._router.read(
                primary_source, num_bytes, timeout=config.server.request_timeout
            )
        except TimeoutError:
            await context.abort(
                grpc.StatusCode.DEADLINE_EXCEEDED, "Request timed out waiting for device"
            )
        except SourceUnavailableError as exc:
            logger.error("Source unavailable: %s", exc)
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except Exception as exc:
            logger.error("Source error: %s", exc)
            await context.abort(grpc.StatusCode.UNAVAILABLE, f"Source error: {exc}")

        timestamp_ns = read.timestamp_ns if self._config.freshness.emit_generation_timestamp else 0

        extras = provenance_extras
        if callable(extras):
            # Draw-path extras depend on the measured bytes (integration
            # statistics); resolved here so the request still produces
            # exactly ONE provenance record. The callable aborts the RPC
            # itself on a draw-specific failure.
            extras = await extras(read)

        try:
            self._router.record_provenance(
                read,
                protocol=protocol,
                served_bytes=num_bytes,
                sequence_id=sequence_id,
                api_key_id=key_info["id"],
                extras=extras,
            )
        except ProvenanceWriteError as exc:
            # provenance.strict: an unrecorded request must not be served.
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

        await self._db.record_usage(key_info["id"], num_bytes)
        return Measurement(
            data=bytes(read.data), device_id=read.source_id, timestamp_ns=timestamp_ns
        )
