"""gRPC server hosting all three wire protocols on one process.

Services registered on the same server (same port(s), same auth, same
limits, same devices):

- ``qrng.QuantumRNG/GetRandomBytes`` — the public, general-purpose API.
  Response: ``data`` + ``timestamp`` (epoch MICROseconds) + ``device_id``.
- ``qr_entropy.EntropyService/GetEntropy`` (unary) and ``StreamEntropy``
  (bidi) — the qr-sampler seam. Response: ``data`` + echoed
  ``sequence_id`` + ``generation_timestamp_ns`` + ``device_id``. The
  ``sequence_id`` echo is what lets qr-sampler's pipelined prefetch
  verify post-selection ordering (``echo_verified``), and the bidi RPC
  is what unlocks its lowest-latency ``bidi_streaming`` mode.
- ``qr_purity.PurityService/GetDraw`` (unary) and ``StreamDraws`` (bidi)
  — the QPI layer: one freshly measured raw block, integrated
  server-side against the source's frozen fingerprint into ``(z, u)``,
  served with the coherence snapshot and the canonical purity label.
  Every draw goes through the SAME :class:`~qbert0g.gate.RequestGate`
  (one validation path), accounting ``block_bytes`` as the served
  quantity.

Binding: TCP (``server.listen``) and/or a UNIX domain socket
(``server.unix_socket``). For entropy-local deployments (e.g. qthought)
bind loopback/UDS only — never ``0.0.0.0`` unless you deliberately want
to serve entropy off-box.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass

import grpc
from grpc import aio

from .coherence import CoherenceMonitor
from .config import Config, IntegrationConfig, PurityConfig
from .database import Database
from .devices import DeviceManager, DeviceState, DeviceStatus
from .fingerprint import Fingerprint, load_config_fingerprints
from .gate import RequestGate
from .integrators import integrate
from .proto import (
    entropy_service_pb2,
    entropy_service_pb2_grpc,
    purity_service_pb2,
    purity_service_pb2_grpc,
    qrng_pb2,
    qrng_pb2_grpc,
)
from .purity import (
    EntropyLabel,
    Integrity,
    Origin,
    derive_static_label,
    resolve_request_bits,
)
from .sources import SourceRead, SourceRouter

logger = logging.getLogger(__name__)


class QuantumRNGServicer(qrng_pb2_grpc.QuantumRNGServicer):
    """The public ``qrng.QuantumRNG`` service."""

    def __init__(self, gate: RequestGate) -> None:
        self._gate = gate

    async def GetRandomBytes(
        self, request: qrng_pb2.RandomRequest, context: grpc.aio.ServicerContext
    ) -> qrng_pb2.RandomResponse:
        m = await self._gate.measure(context, request.num_bytes, protocol="qrng")
        return qrng_pb2.RandomResponse(
            data=m.data,
            timestamp=m.timestamp_ns // 1_000,  # epoch microseconds
            device_id=m.device_id,
        )


class EntropyServicer(entropy_service_pb2_grpc.EntropyServiceServicer):
    """The ``qr_entropy.EntropyService`` — qr-sampler's native protocol.

    Echoes ``sequence_id`` verbatim (qr-sampler sends a 63-bit commitment
    nonce there on the pipelined path) and stamps
    ``generation_timestamp_ns`` at measurement time.
    """

    def __init__(self, gate: RequestGate) -> None:
        self._gate = gate

    async def GetEntropy(
        self,
        request: entropy_service_pb2.EntropyRequest,
        context: grpc.aio.ServicerContext,
    ) -> entropy_service_pb2.EntropyResponse:
        m = await self._gate.measure(
            context, request.bytes_needed, protocol="qr_entropy", sequence_id=request.sequence_id
        )
        return entropy_service_pb2.EntropyResponse(
            data=m.data,
            sequence_id=request.sequence_id,
            generation_timestamp_ns=m.timestamp_ns,
            device_id=m.device_id,
        )

    async def StreamEntropy(self, request_iterator, context: grpc.aio.ServicerContext):
        """Persistent bidirectional stream: one response per request.

        Also serves qr-sampler's ``server_streaming`` mode (one request,
        one response, client cancels). Each in-stream request goes
        through the full gate — auth, rate limit, caps — exactly like a
        unary call.
        """
        async for request in request_iterator:
            m = await self._gate.measure(
                context,
                request.bytes_needed,
                protocol="qr_entropy",
                sequence_id=request.sequence_id,
            )
            yield entropy_service_pb2.EntropyResponse(
                data=m.data,
                sequence_id=request.sequence_id,
                generation_timestamp_ns=m.timestamp_ns,
                device_id=m.device_id,
            )


@dataclass(frozen=True)
class QpiContext:
    """Frozen composition of everything the PurityService needs.

    Built once in :meth:`QbertServer.start` — fingerprints (with their
    file hashes) and static labels are frozen at load per the QPI
    invariants; the monitor is the only live object and is consulted
    read-only via :meth:`~qbert0g.coherence.CoherenceMonitor.snapshot`.
    """

    fingerprints: dict[str, tuple[Fingerprint, str]]  # source id -> (fp, file sha256)
    labels: dict[str, EntropyLabel]  # source id -> static label (startup-derived)
    integration: IntegrationConfig
    purity: PurityConfig
    devices: DeviceManager  # health snapshots for the quantum_verified bit
    monitor: CoherenceMonitor | None  # None unless coherence.enabled


def build_qpi_context(
    config: Config, devices: DeviceManager, monitor: CoherenceMonitor | None
) -> QpiContext:
    """Load fingerprints and derive static labels for every drawable source.

    The startup half of FR-Q3 lives in
    :func:`~qbert0g.fingerprint.load_config_fingerprints`: a missing or
    invalid fingerprint file raises ``ConfigError`` and the server does
    not start.
    """
    fingerprints = load_config_fingerprints(config)
    device_cfg = {d.id: d for d in config.devices}
    labels: dict[str, EntropyLabel] = {}
    for source_id in config.integration.sources:
        fp = fingerprints[source_id][0]
        dev = device_cfg.get(source_id)
        if dev is not None:
            # chardev has no qcc-cli post-processing chain (mode: None).
            mode = None if dev.type == "chardev" else (
                dev.post_processing or config.post_processing_mode
            )
            labels[source_id] = derive_static_label(
                "device", device_type=dev.type, post_processing_mode=mode, fingerprint=fp
            )
        else:  # config validation guarantees: device or control
            labels[source_id] = derive_static_label("control", fingerprint=fp)
    return QpiContext(
        fingerprints=fingerprints,
        labels=labels,
        integration=config.integration,
        purity=config.purity,
        devices=devices,
        monitor=monitor,
    )


def health_clean(state: DeviceState | None) -> bool:
    """Was the device healthy at measurement time (quantum_verified input)?

    Chardev devices additionally consult the pre-measurement sysfs
    health snapshot (``error_present`` raw text: ``"0"``/empty = clean).
    Non-device sources (controls: no :class:`DeviceState`) are not
    health-checkable — ``False`` (they can never be quantum_verified
    anyway: origin ``pseudo``).
    """
    if state is None:
        return False
    if state.status not in (DeviceStatus.ONLINE, DeviceStatus.BUSY):
        return False
    return not (
        state.config.type == "chardev" and state.last_error_present not in (None, "", "0")
    )


def quantum_verified(
    static: EntropyLabel, state: DeviceState | None, z: float, verify_sigma: float
) -> bool:
    """The per-request ``quantum_verified`` bit (spec §3.5).

    True iff origin quantum AND integrity intact AND the health snapshot
    at measurement is clean AND the live block statistic is within the
    fingerprint tolerance (``|z| <= purity.verify_sigma``).
    """
    return (
        static.origin is Origin.QUANTUM
        and static.integrity is Integrity.INTACT
        and health_clean(state)
        and abs(z) <= verify_sigma
    )


class PurityServicer(purity_service_pb2_grpc.PurityServiceServicer):
    """The ``qr_purity.PurityService`` — server-side integrated draws.

    Per-request flow (StreamDraws runs the same body per in-stream
    request): validate the requested source / block size, measure
    through the shared :class:`RequestGate` (one validation path; the
    accounted quantity is ``block_bytes``), integrate the raw block
    (primary + configured secondaries) off the event loop, and respond
    with the echoed ``sequence_id``, the clamped ``u``, the per-request
    purity label bits, and the coherence snapshot
    (``coherence_valid=False`` whenever absent, stale, or never
    computed — never a fake zero).
    """

    def __init__(self, gate: RequestGate, qpi: QpiContext) -> None:
        self._gate = gate
        self._qpi = qpi

    async def GetDraw(
        self, request: purity_service_pb2.DrawRequest, context: grpc.aio.ServicerContext
    ) -> purity_service_pb2.DrawResponse:
        return await self._draw(request, context)

    async def StreamDraws(self, request_iterator, context: grpc.aio.ServicerContext):
        """Persistent bidirectional stream: one draw per request.

        Each in-stream request goes through the full gate — auth, rate
        limit, caps — exactly like a unary call.
        """
        async for request in request_iterator:
            yield await self._draw(request, context)

    async def _draw(
        self, request: purity_service_pb2.DrawRequest, context: grpc.aio.ServicerContext
    ) -> purity_service_pb2.DrawResponse:
        qpi = self._qpi
        if not qpi.integration.sources:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "PurityService is not configured: integration.sources is empty",
            )
        source_id = request.source_id or None
        if source_id is not None and source_id not in qpi.integration.sources:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"source {source_id!r} is not drawable (not in integration.sources)",
            )
        block_bytes = int(request.block_bytes) or qpi.integration.block_bytes

        box: dict = {}  # results the response needs, filled by draw_extras

        async def draw_extras(read: SourceRead) -> dict:
            """Integrate + label the measured block; provenance extras.

            Runs inside :meth:`RequestGate.measure`, between the read
            and the (single) provenance write, so the record carries the
            integration facts. Aborts the RPC on a draw-specific failure
            (e.g. failover served a device without a fingerprint).
            """
            serving_id = read.source_id
            entry = qpi.fingerprints.get(serving_id)
            if entry is None:
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    f"serving source {serving_id!r} has no loaded fingerprint — "
                    "draws require a source in integration.sources (key binding or "
                    "failover resolved outside the drawable set)",
                )
            fp, fp_sha256 = entry
            names = [qpi.integration.default_integrator, *qpi.integration.secondaries]
            # CPU-bound numpy/int work on up to 2 MiB — off the event loop
            # (precedent: markov generation offload in sources.py).
            results = await asyncio.to_thread(
                lambda: [integrate(name, read.data, fp) for name in names]
            )
            primary = results[0]
            secondaries = {
                name: result.aux
                for name, result in zip(names[1:], results[1:], strict=True)
            }
            static = qpi.labels[serving_id]
            state = qpi.devices.devices.get(serving_id)
            verified = quantum_verified(static, state, primary.z, qpi.purity.verify_sigma)
            label = resolve_request_bits(
                static, integration_n=len(read.data), quantum_verified=verified
            )
            if qpi.monitor is not None:
                coherence_value, coherence_valid = qpi.monitor.snapshot()
            else:
                coherence_value, coherence_valid = None, False
            box.update(
                primary=primary,
                label=label,
                integrator=names[0],
                integrated_bytes=len(read.data),
                coherence_value=coherence_value,
                coherence_valid=coherence_valid,
            )
            coherence_extra = None
            if coherence_value is not None:
                age_ms = (
                    time.monotonic_ns() - coherence_value.computed_monotonic_ns
                ) / 1e6
                coherence_extra = {
                    "r": coherence_value.r,
                    "lag": coherence_value.lag,
                    "z_c": coherence_value.z_c,
                    "valid": coherence_valid,
                    "age_ms": age_ms,
                }
            return {
                "integrator": names[0],
                "integrated_bytes": len(read.data),
                "z": primary.z,
                "secondaries": secondaries,
                "purity_label": label.canonical(),
                "fingerprint_sha256": fp_sha256,
                "coherence": coherence_extra,
            }

        m = await self._gate.measure(
            context,
            block_bytes,
            protocol="qr_purity",
            sequence_id=request.sequence_id,
            requested_source_id=source_id,
            provenance_extras=draw_extras,
        )

        primary = box["primary"]
        coherence_value = box["coherence_value"]
        return purity_service_pb2.DrawResponse(
            u=primary.u,
            z=primary.z,
            sequence_id=request.sequence_id,  # echoed verbatim
            generation_timestamp_ns=m.timestamp_ns,
            source_id=m.device_id,
            coherence_z=coherence_value.z_c if coherence_value is not None else 0.0,
            coherence_valid=box["coherence_valid"],
            purity_label=box["label"].canonical(),
            integrated_bytes=box["integrated_bytes"],
            integrator=box["integrator"],
            coherence_r=coherence_value.r if coherence_value is not None else 0.0,
        )


class QbertServer:
    """Owns the database, devices, and the aio gRPC server lifecycle.

    Separated from :func:`serve` so tests can start/stop a fully wired
    server on an ephemeral port without signal handling.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.database_path)
        self.devices = DeviceManager(config)
        self.router: SourceRouter | None = None  # built in start() (controls need model files)
        self.qpi: QpiContext | None = None  # built in start() (fingerprints need files)
        self.coherence_monitor: CoherenceMonitor | None = None  # when coherence.enabled
        self.port: int | None = None  # actual TCP port after start()
        self._server: aio.Server | None = None

    async def start(self) -> None:
        """Connect the DB, initialize devices, bind, and start serving."""
        config = self.config

        await self.db.connect(bootstrap_admin_key=config.auth.api_key)
        logger.info("Database connected (%s)", config.database_path)

        await self.devices.initialize()
        logger.info("Initialized %d device(s)", len(self.devices.devices))

        # Fails loudly on a broken control (e.g. missing Markov model):
        # never start serving with a silently absent experiment arm.
        self.router = SourceRouter(config, self.devices)
        logger.info(
            "Source namespace: %d device(s), %d control(s), %d profile(s)",
            len(self.devices.devices),
            len(config.controls),
            len(config.profiles),
        )

        server = aio.server(
            options=[
                ("grpc.max_send_message_length", config.server.max_message_size),
                ("grpc.max_receive_message_length", config.server.max_message_size),
            ]
        )
        gate = RequestGate(config, self.db, self.router)
        qrng_pb2_grpc.add_QuantumRNGServicer_to_server(QuantumRNGServicer(gate), server)
        entropy_service_pb2_grpc.add_EntropyServiceServicer_to_server(
            EntropyServicer(gate), server
        )

        # QPI layer: fingerprints load-or-refuse at startup (FR-Q3);
        # the coherence monitor exists only when coherence.enabled.
        if config.coherence.enabled:
            self.coherence_monitor = CoherenceMonitor(config.coherence, self.router)
        self.qpi = build_qpi_context(config, self.devices, self.coherence_monitor)
        purity_service_pb2_grpc.add_PurityServiceServicer_to_server(
            PurityServicer(gate, self.qpi), server
        )
        if self.qpi.integration.sources:
            logger.info(
                "PurityService: %d drawable source(s), default integrator %s, "
                "block %d bytes",
                len(self.qpi.integration.sources),
                config.integration.default_integrator,
                config.integration.block_bytes,
            )

        if config.server.listen:
            host = config.server.listen.rsplit(":", 1)[0]
            if host in ("0.0.0.0", "[::]", "::"):
                logger.warning(
                    "Binding %s exposes the entropy service off-box. For local-only "
                    "deployments bind 127.0.0.1 or a UNIX socket.",
                    config.server.listen,
                )
            self.port = server.add_insecure_port(config.server.listen)
            logger.info("Listening on %s (port %d)", config.server.listen, self.port)
        if config.server.unix_socket:
            if sys.platform == "win32":
                logger.warning("UNIX domain sockets are unsupported on Windows; skipping %s",
                               config.server.unix_socket)
            else:
                server.add_insecure_port(f"unix:{config.server.unix_socket}")
                logger.info("Listening on unix:%s", config.server.unix_socket)

        await server.start()
        self._server = server
        if self.coherence_monitor is not None:
            self.coherence_monitor.start()
        logger.info("gRPC server started (QuantumRNG + EntropyService + PurityService)")

    async def stop(self, grace: float | None = None) -> None:
        """Stop serving, shut devices down, close the database."""
        if grace is None:
            grace = self.config.server.request_timeout
        if self.coherence_monitor is not None:
            await self.coherence_monitor.stop()
        if self._server is not None:
            await self._server.stop(grace=grace)
            self._server = None
        await self.devices.shutdown()
        await self.db.disconnect()


async def serve(config: Config) -> None:
    """Run the server until SIGINT/SIGTERM."""
    server = QbertServer(config)
    await server.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    except NotImplementedError:  # Windows dev box: Ctrl+C raises instead
        pass

    with contextlib.suppress(KeyboardInterrupt):
        await stop_event.wait()

    grace = config.server.request_timeout
    logger.info("Shutting down — allowing up to %.0fs for in-flight requests...", grace)
    try:
        # The outer wait_for guards against gRPC C-core threads stalling
        # internally (historically caused shutdown hangs).
        await asyncio.wait_for(server.stop(grace=grace), timeout=grace + 2.0)
        logger.info("Shutdown complete")
    except TimeoutError:
        logger.warning("Graceful shutdown timed out, forcing exit")

    # Bypass threading._shutdown(), which would otherwise block on
    # non-daemon gRPC/device I/O threads.
    os._exit(0)
