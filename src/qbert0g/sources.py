"""SourceRouter — one id namespace over devices, PRNG controls, and profiles.

The router is the single read seam behind :class:`~qbert0g.gate.RequestGate`:

- **Plain device ids** delegate to :meth:`DeviceManager.read_bytes`
  unchanged — failover, locking and freshness behave exactly as before
  the router existed.
- **Control ids** serve seeded PRNG bytes (:mod:`.controls`), serialized
  per control so ``stream_offset_bytes`` always describes a contiguous
  block (the offline-regeneration contract).
- **Profile ids** read their inputs and apply the transform
  (:mod:`.profiles`). Profiles NEVER fail over: an unavailable input
  fails the request (``SourceUnavailableError`` → gRPC ``UNAVAILABLE``)
  — an experiment arm must never silently change composition.

Paired quantum-quantum reads (xnor of two devices) acquire the two
device locks in lexicographic id order (fixed deadlock rule), flush each
device once at request start, then read alternating chunks of
``profiles_defaults.chunk_bytes`` with a monotonic-ns timestamp per
chunk. ``max_pair_skew_ns`` over ``profiles_defaults.max_skew_ns`` is
served anyway but logged at WARNING — the data is not wrong, the
simultaneity bound is just looser than intended.

Every served request also produces one provenance record
(:class:`ProvenanceLog`, append-only JSONL): per-input ``kind``
(``"quantum"`` / ``"prng"`` / ``"mock"``) makes any pseudorandom or mock
involvement explicit and greppable. A provenance write failure never
fails the request unless ``provenance.strict`` is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import Config
from .controls import PrngMarkovControl, PrngUniformControl, make_control
from .devices import DeviceManager, DeviceState, DeviceStatus
from .profiles import Profile

logger = logging.getLogger(__name__)


class SourceUnavailableError(RuntimeError):
    """A profile input (or the source itself) is unavailable — no failover."""


class ProvenanceWriteError(RuntimeError):
    """Provenance write failed with ``provenance.strict: true`` set."""


@dataclass(frozen=True)
class SourceRead:
    """One completed read through the router, with its provenance facts."""

    data: bytes
    source_id: str  # serving id: profile/control id, or actual device after failover
    timestamp_ns: int  # epoch ns of the last contributing measurement
    transform: str | None  # profile transform name; None for plain devices/controls
    inputs: list[dict]  # per-input provenance facts (JSON-ready)
    max_pair_skew_ns: int | None  # paired device reads only


class ProvenanceLog:
    """Append-only JSONL, one record per served request.

    Writes are small and synchronous (open-append-close per record) so a
    crashed server never holds a half-written buffer. A write failure
    logs ERROR and the request proceeds — unless ``strict``, which
    raises :class:`ProvenanceWriteError` so the gate fails the request
    (study runs must not serve unrecorded bytes).

    Deferred (spec §12): a rolling statistics monitor (per-source
    bit-position / ACF windows) would hook here; the JSONL already
    carries enough to compute those offline.
    """

    def __init__(self, path: str, strict: bool) -> None:
        self._path = path
        self._strict = strict

    def write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"))
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            if self._strict:
                raise ProvenanceWriteError(
                    f"provenance write to {self._path} failed (strict mode): {exc}"
                ) from exc
            logger.error(
                "Provenance write to %s failed (request served anyway): %s", self._path, exc
            )


class SourceRouter:
    """Resolve any source id — device, control, or profile — to bytes.

    Constructed at server startup AFTER ``DeviceManager.initialize()``;
    control construction fails loudly here (missing/invalid Markov
    model), consistent with the never-start-on-silent-defaults stance.
    """

    def __init__(self, config: Config, devices: DeviceManager) -> None:
        self._config = config
        self._devices = devices
        self._controls: dict[str, PrngUniformControl | PrngMarkovControl] = {
            ctl.id: make_control(ctl) for ctl in config.controls
        }
        # Per-control serialization: stream_offset_bytes must describe a
        # contiguous block even under concurrent requests.
        self._control_locks = {cid: asyncio.Lock() for cid in self._controls}
        self._profiles = {prof.id: Profile(prof) for prof in config.profiles}
        self._chunk_bytes = config.profiles_defaults.chunk_bytes
        self._max_skew_ns = config.profiles_defaults.max_skew_ns
        self.provenance = ProvenanceLog(config.provenance.path, config.provenance.strict)

    # ── resolution ─────────────────────────────────────────────────────

    def default_device_id(self) -> str | None:
        """First initialized device — the ``*`` key binding target."""
        return next(iter(self._devices.devices), None)

    async def read(
        self, source_id: str, num_bytes: int, timeout: float | None = None
    ) -> SourceRead:
        """Read *num_bytes* from any source id.

        Deferred (spec §12): blinded arm aliases (opaque ids mapping to
        profiles, sealed mapping) would resolve here, before the lookup.
        """
        if source_id in self._profiles:
            coro = self._read_profile(self._profiles[source_id], num_bytes)
            if timeout is None:
                return await coro
            return await asyncio.wait_for(coro, timeout)
        if source_id in self._controls:
            return await self._read_control(source_id, num_bytes)
        return await self._read_device(source_id, num_bytes, timeout)

    # ── plain devices (delegation — behavior unchanged) ────────────────

    async def _read_device(
        self, device_id: str, num_bytes: int, timeout: float | None
    ) -> SourceRead:
        if timeout is None:
            timeout = self._config.server.request_timeout
        data, serving_id = await self._devices.read_bytes(device_id, num_bytes, timeout=timeout)
        timestamp_ns = time.time_ns()
        state = self._devices.devices.get(serving_id)
        fact = {"id": serving_id, "kind": self._device_kind(serving_id), "raw_bytes": num_bytes}
        if state is not None:
            _attach_chardev_health(fact, state)
        return SourceRead(
            data=data,
            source_id=serving_id,
            timestamp_ns=timestamp_ns,
            transform=None,
            inputs=[fact],
            max_pair_skew_ns=None,
        )

    def _device_kind(self, device_id: str) -> str:
        state = self._devices.devices.get(device_id)
        if state is not None and state.config.type == "mock":
            return "mock"  # the mock decision is recorded, never laundered
        return "quantum"

    # ── controls ────────────────────────────────────────────────────────

    async def _read_control(self, control_id: str, num_bytes: int) -> SourceRead:
        data, fact = await self._control_bytes(control_id, num_bytes)
        return SourceRead(
            data=data,
            source_id=control_id,
            timestamp_ns=time.time_ns(),
            transform=None,
            inputs=[fact],
            max_pair_skew_ns=None,
        )

    async def _control_bytes(self, control_id: str, num_bytes: int) -> tuple[bytes, dict]:
        """Generate control bytes + the provenance fact that regenerates them."""
        control = self._controls[control_id]
        async with self._control_locks[control_id]:
            offset_before = control.stream_offset_bytes
            # Markov generation is one python-loop step per byte — keep
            # the event loop responsive for large requests.
            data = await asyncio.to_thread(control.read, num_bytes)
        fact = {
            "id": control_id,
            "kind": "prng",
            "raw_bytes": num_bytes,
            "seed": control.config.seed,
            "stream_offset_bytes": offset_before,
        }
        if isinstance(control, PrngMarkovControl):
            fact["model_sha256"] = control.model_sha256
        return data, fact

    # ── profiles ────────────────────────────────────────────────────────

    async def _read_profile(self, profile: Profile, num_bytes: int) -> SourceRead:
        need = profile.raw_bytes_needed(num_bytes)
        device_inputs = [i for i in profile.inputs if i not in self._controls]

        # Profiles never fail over: every device input must be usable NOW.
        for device_id in device_inputs:
            state = self._devices.devices.get(device_id)
            if state is None or state.status not in (DeviceStatus.ONLINE, DeviceStatus.BUSY):
                raise SourceUnavailableError(
                    f"profile {profile.id!r}: input device {device_id!r} is unavailable "
                    "(profiles never fail over — arm composition is fixed)"
                )

        if len(device_inputs) == 2:
            raw, facts, timestamp_ns, max_skew = await self._paired_device_read(profile, need)
        else:
            raw, facts, timestamp_ns = await self._sequential_read(profile, need)
            max_skew = None

        data = profile.apply(raw, num_bytes)
        if not data:
            raise SourceUnavailableError(f"profile {profile.id!r} produced no data")
        return SourceRead(
            data=data,
            source_id=profile.id,
            timestamp_ns=timestamp_ns,
            transform=profile.transform,
            inputs=facts,
            max_pair_skew_ns=max_skew,
        )

    async def _sequential_read(
        self, profile: Profile, need: int
    ) -> tuple[list[bytes], list[dict], int]:
        """Inputs with at most one device: quantum first, then controls.

        No pairing constraints apply (spec §7) — the device contributes
        the ``generation_timestamp_ns`` instant; pure-PRNG profiles are
        stamped at generation completion time.
        """
        raw: dict[str, bytes] = {}
        facts: dict[str, dict] = {}
        timestamp_ns = 0
        for input_id in profile.inputs:
            if input_id not in self._controls:  # the (single) device input, read first
                raw[input_id], facts[input_id], timestamp_ns = await self._device_input_read(
                    input_id, need
                )
        for input_id in profile.inputs:
            if input_id in self._controls:
                raw[input_id], facts[input_id] = await self._control_bytes(input_id, need)
        if timestamp_ns == 0:
            timestamp_ns = time.time_ns()  # pure-PRNG: generation completion time
        return (
            [raw[i] for i in profile.inputs],
            [facts[i] for i in profile.inputs],
            timestamp_ns,
        )

    async def _device_input_read(self, device_id: str, need: int) -> tuple[bytes, dict, int]:
        """One full profile-input read from a single device (no failover)."""
        try:
            async with self._devices.acquire_for_profile(device_id) as state:
                mono_ns = time.monotonic_ns()
                data = await self._devices.read_chunk_locked(device_id, need)
                mono_end = time.monotonic_ns()
                epoch_ns = time.time_ns()
                fact = self._device_fact(device_id, state, need, mono_ns, mono_end)
        except SourceUnavailableError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(
                f"profile input device {device_id!r} failed: {exc}"
            ) from exc
        return data, fact, epoch_ns

    async def _paired_device_read(
        self, profile: Profile, need: int
    ) -> tuple[list[bytes], list[dict], int, int]:
        """Paired quantum-quantum read: locked, flushed once, chunk-alternated."""
        a_id, b_id = profile.inputs
        first_id, second_id = sorted((a_id, b_id))  # lexicographic lock order (deadlock rule)
        dm = self._devices
        chunks: dict[str, list[bytes]] = {a_id: [], b_id: []}
        stamps: dict[str, list[int]] = {a_id: [], b_id: []}
        try:
            async with (
                dm.acquire_for_profile(first_id) as first_state,
                dm.acquire_for_profile(second_id) as second_state,
            ):
                remaining = need
                while remaining:
                    n = min(self._chunk_bytes, remaining)
                    # Read order follows the TRANSFORM input order (A, B, A, B...);
                    # only lock ACQUISITION is lexicographic.
                    for input_id in (a_id, b_id):
                        chunks[input_id].append(await dm.read_chunk_locked(input_id, n))
                        stamps[input_id].append(time.monotonic_ns())
                    remaining -= n
                epoch_ns = time.time_ns()  # instant of the last contributing raw chunk
                states = {first_id: first_state, second_id: second_state}
                facts = [
                    self._device_fact(i, states[i], need, stamps[i][0], stamps[i][-1])
                    for i in (a_id, b_id)
                ]
        except SourceUnavailableError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(
                f"profile {profile.id!r}: paired read failed: {exc}"
            ) from exc

        max_skew = max(abs(ta - tb) for ta, tb in zip(stamps[a_id], stamps[b_id], strict=True))
        if max_skew > self._max_skew_ns:
            logger.warning(
                "Profile %s: pair skew %d ns exceeds max_skew_ns=%d — serving anyway; "
                "the simultaneity bound is looser than intended",
                profile.id,
                max_skew,
                self._max_skew_ns,
            )
        return [b"".join(chunks[a_id]), b"".join(chunks[b_id])], facts, epoch_ns, max_skew

    def _device_fact(
        self, device_id: str, state: DeviceState, raw_bytes: int, first_ns: int, last_ns: int
    ) -> dict:
        fact = {
            "id": device_id,
            "kind": self._device_kind(device_id),
            "raw_bytes": raw_bytes,
            "first_chunk_ns": first_ns,  # monotonic ns (skew basis)
            "last_chunk_ns": last_ns,
        }
        _attach_chardev_health(fact, state)
        return fact

    # ── provenance ──────────────────────────────────────────────────────

    def record_provenance(
        self,
        read: SourceRead,
        *,
        protocol: str,
        served_bytes: int,
        sequence_id: int | None = None,
        api_key_id: str | None = None,
    ) -> dict:
        """Compose and append the per-request provenance record.

        Raises :class:`ProvenanceWriteError` only in strict mode. Plain
        device requests get records too — they are sources like any other.
        """
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "request_id": uuid.uuid4().hex,
            "source_id": read.source_id,
            "protocol": protocol,
            "sequence_id": sequence_id,
            "served_bytes": served_bytes,
            "transform": read.transform,
            "inputs": read.inputs,
            "max_pair_skew_ns": read.max_pair_skew_ns,
            "api_key_id": api_key_id,
        }
        self.provenance.write(record)
        return record

    # ── introspection (CLI `sources list`) ──────────────────────────────

    def describe(self) -> list[dict]:
        """One row per source: id, kind, transform, inputs, availability."""
        rows = []
        for device_id, state in self._devices.devices.items():
            rows.append(
                {
                    "id": device_id,
                    "kind": self._device_kind(device_id),
                    "transform": "",
                    "inputs": "",
                    "availability": state.status.value,
                }
            )
        for control_id, control in self._controls.items():
            rows.append(
                {
                    "id": control_id,
                    "kind": "prng",
                    "transform": control.config.type,
                    "inputs": "",
                    "availability": "ready",
                }
            )
        for profile in self._profiles.values():
            unavailable = [
                i
                for i in profile.inputs
                if i not in self._controls
                and (
                    i not in self._devices.devices
                    or self._devices.devices[i].status
                    not in (DeviceStatus.ONLINE, DeviceStatus.BUSY)
                )
            ]
            rows.append(
                {
                    "id": profile.id,
                    "kind": "profile",
                    "transform": profile.transform,
                    "inputs": ",".join(profile.inputs),
                    "availability": (
                        "ready" if not unavailable else f"unavailable ({','.join(unavailable)})"
                    ),
                }
            )
        return rows


def _attach_chardev_health(fact: dict, state: DeviceState) -> None:
    """Chardev pre-measurement snapshot (S8) → provenance fact fields."""
    if state.config.type != "chardev":
        return
    if state.last_flushed_bytes is not None:
        fact["flushed_bytes"] = state.last_flushed_bytes
    if state.last_error_present is not None:
        fact["error_present"] = state.last_error_present
    if state.last_error_bits is not None:
        fact["error_bits"] = state.last_error_bits
