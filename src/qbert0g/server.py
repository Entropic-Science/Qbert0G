"""gRPC server hosting both wire protocols on one process.

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

import grpc
from grpc import aio

from .config import Config
from .database import Database
from .devices import DeviceManager
from .gate import RequestGate
from .proto import entropy_service_pb2, entropy_service_pb2_grpc, qrng_pb2, qrng_pb2_grpc

logger = logging.getLogger(__name__)


class QuantumRNGServicer(qrng_pb2_grpc.QuantumRNGServicer):
    """The public ``qrng.QuantumRNG`` service."""

    def __init__(self, gate: RequestGate) -> None:
        self._gate = gate

    async def GetRandomBytes(
        self, request: qrng_pb2.RandomRequest, context: grpc.aio.ServicerContext
    ) -> qrng_pb2.RandomResponse:
        m = await self._gate.measure(context, request.num_bytes)
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
        m = await self._gate.measure(context, request.bytes_needed)
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
            m = await self._gate.measure(context, request.bytes_needed)
            yield entropy_service_pb2.EntropyResponse(
                data=m.data,
                sequence_id=request.sequence_id,
                generation_timestamp_ns=m.timestamp_ns,
                device_id=m.device_id,
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
        self.port: int | None = None  # actual TCP port after start()
        self._server: aio.Server | None = None

    async def start(self) -> None:
        """Connect the DB, initialize devices, bind, and start serving."""
        config = self.config

        await self.db.connect(bootstrap_admin_key=config.auth.api_key)
        logger.info("Database connected (%s)", config.database_path)

        await self.devices.initialize()
        logger.info("Initialized %d device(s)", len(self.devices.devices))

        server = aio.server(
            options=[
                ("grpc.max_send_message_length", config.server.max_message_size),
                ("grpc.max_receive_message_length", config.server.max_message_size),
            ]
        )
        gate = RequestGate(config, self.db, self.devices)
        qrng_pb2_grpc.add_QuantumRNGServicer_to_server(QuantumRNGServicer(gate), server)
        entropy_service_pb2_grpc.add_EntropyServiceServicer_to_server(
            EntropyServicer(gate), server
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
        logger.info("gRPC server started (QuantumRNG + EntropyService)")

    async def stop(self, grace: float | None = None) -> None:
        """Stop serving, shut devices down, close the database."""
        if grace is None:
            grace = self.config.server.request_timeout
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
