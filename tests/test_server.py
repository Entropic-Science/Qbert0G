"""End-to-end tests of both gRPC services over loopback (mock device).

Uses ``grpc.aio`` client stubs — the server runs on the test's event
loop, so a blocking sync client here would deadlock.
"""

import time

import grpc
import pytest
from conftest import ADMIN_KEY

from qbert0g.proto import (
    entropy_service_pb2,
    entropy_service_pb2_grpc,
    qrng_pb2,
    qrng_pb2_grpc,
)

METADATA = (("api-key", ADMIN_KEY),)


class TestQuantumRNG:
    """The public ``qrng.QuantumRNG`` protocol."""

    async def test_get_random_bytes(self, address):
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            response = await stub.GetRandomBytes(
                qrng_pb2.RandomRequest(num_bytes=64), metadata=METADATA
            )
        assert len(response.data) == 64
        assert response.device_id == "mock-0"
        # timestamp is epoch MICROseconds, stamped at measurement time
        assert abs(response.timestamp / 1e6 - time.time()) < 60

    async def test_missing_api_key_unauthenticated(self, address):
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetRandomBytes(qrng_pb2.RandomRequest(num_bytes=8))
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_invalid_api_key_unauthenticated(self, address):
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetRandomBytes(
                    qrng_pb2.RandomRequest(num_bytes=8), metadata=(("api-key", "wrong"),)
                )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_zero_bytes_invalid(self, address):
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetRandomBytes(
                    qrng_pb2.RandomRequest(num_bytes=0), metadata=METADATA
                )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_over_per_request_cap_invalid(self, address):
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetRandomBytes(
                    qrng_pb2.RandomRequest(num_bytes=16385), metadata=METADATA
                )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT


class TestEntropyService:
    """The ``qr_entropy.EntropyService`` protocol (qr-sampler's native seam)."""

    async def test_get_entropy_echoes_sequence_id(self, address):
        nonce = 0x7FEDCBA987654321  # a 63-bit commitment nonce, like qr-sampler sends
        async with grpc.aio.insecure_channel(address) as channel:
            stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
            reply = await stub.GetEntropy(
                entropy_service_pb2.EntropyRequest(bytes_needed=128, sequence_id=nonce),
                metadata=METADATA,
            )
        assert len(reply.data) == 128
        assert reply.sequence_id == nonce  # echo — enables echo_verified
        assert reply.device_id == "mock-0"
        assert abs(reply.generation_timestamp_ns / 1e9 - time.time()) < 60

    async def test_get_entropy_shares_auth_with_qrng(self, address):
        async with grpc.aio.insecure_channel(address) as channel:
            stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetEntropy(
                    entropy_service_pb2.EntropyRequest(bytes_needed=8)
                )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_stream_entropy_bidi_correlates_by_echo(self, address):
        nonces = [101, 202, 303]
        async with grpc.aio.insecure_channel(address) as channel:
            stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
            call = stub.StreamEntropy(metadata=METADATA)
            replies = []
            for nonce in nonces:
                await call.write(
                    entropy_service_pb2.EntropyRequest(bytes_needed=32, sequence_id=nonce)
                )
                replies.append(await call.read())
            await call.done_writing()
        assert [r.sequence_id for r in replies] == nonces
        assert all(len(r.data) == 32 for r in replies)

    async def test_both_services_share_usage_accounting(self, server, address):
        """A read through either protocol lands in the same usage ledger."""
        async with grpc.aio.insecure_channel(address) as channel:
            qrng_stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            entropy_stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
            await qrng_stub.GetRandomBytes(
                qrng_pb2.RandomRequest(num_bytes=10), metadata=METADATA
            )
            await entropy_stub.GetEntropy(
                entropy_service_pb2.EntropyRequest(bytes_needed=20), metadata=METADATA
            )
        key = await server.db.validate_api_key(ADMIN_KEY)
        usage = await server.db.get_usage_today(key["id"])
        assert usage["bytes_served"] == 30
        assert usage["requests"] == 2


class TestFreshnessConfig:
    async def test_timestamps_zero_when_emission_disabled(self, tmp_path):
        from conftest import make_config

        from qbert0g.server import QbertServer

        config = make_config(tmp_path, freshness={"emit_generation_timestamp": False})
        srv = QbertServer(config)
        await srv.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{srv.port}") as channel:
                stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
                reply = await stub.GetEntropy(
                    entropy_service_pb2.EntropyRequest(bytes_needed=8), metadata=METADATA
                )
            assert reply.generation_timestamp_ns == 0
        finally:
            await srv.stop(grace=0.5)
