"""Cross-repo seam: qr-sampler's ``QuantumGrpcSource`` against a live Qbert0G.

Skipped when qr-sampler is not installed (it lives in a sibling repo:
``pip install -e ../qr-sampler``). When present, this proves the
"seamless" contract end-to-end:

- qr-sampler's DEFAULT method paths (``/qr_entropy.EntropyService/...``)
  resolve against Qbert0G — zero client configuration beyond address+key.
- The pipelined prefetch path gets a true ``sequence_id`` echo, so
  ``echo_verified`` is True (impossible against the legacy protocol).
- ``bidi_streaming`` mode works over ``StreamEntropy``.
- The legacy ``/qrng.QuantumRNG/GetRandomBytes`` path still serves
  qr-sampler's protocol-agnostic unary client (documented field
  collision: no echo, that's expected).

The source is sync and runs its own background gRPC loop; the Qbert
server runs on the test's event loop — so every blocking source call is
pushed into an executor thread to keep the server responsive.
"""

import asyncio
import functools

import pytest

qr_sampler = pytest.importorskip("qr_sampler")

from conftest import ADMIN_KEY  # noqa: E402
from qr_sampler.contract import QRSamplerConfig  # noqa: E402
from qr_sampler.entropy.qgrpc.source import QuantumGrpcSource  # noqa: E402


def _source(address: str, **overrides) -> QuantumGrpcSource:
    config = QRSamplerConfig(
        grpc_server_address=address,
        grpc_api_key=ADMIN_KEY,
        fallback_mode="error",  # failures must surface, never mask
        **overrides,
    )
    return QuantumGrpcSource(config)


async def _call(fn, /, *args, **kwargs):
    """Run a blocking qr-sampler call off the server's event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


class TestDefaultMethodPaths:
    """qr-sampler with ZERO method-path configuration — the seamless case."""

    async def test_unary_fetch_on_default_path(self, address):
        source = _source(address)
        try:
            data = await _call(source.get_random_bytes, 256)
            assert len(data) == 256
        finally:
            await _call(source.close)

    async def test_health_check_reports_authenticated(self, address):
        source = _source(address)
        try:
            await _call(source.warmup)
            health = await _call(source.health_check)
            assert health["authenticated"] is True
        finally:
            await _call(source.close)

    async def test_pipelined_prefetch_is_echo_verified(self, address):
        """The whole point of serving EntropyService natively: the
        commitment nonce comes back, proving post-selection ordering."""
        source = _source(address)
        try:
            await _call(source.warmup)
            ticket = await _call(source.prefetch, 64, 0x0123456789ABCDEF)
            assert ticket is not None
            data = await _call(source.get_random_bytes_with_ticket, 64, ticket)
            assert len(data) == 64
            assert ticket.echo_verified is True
        finally:
            await _call(source.close)

    async def test_bidi_streaming_mode(self, address):
        source = _source(address, grpc_mode="bidi_streaming")
        try:
            for _ in range(3):
                data = await _call(source.get_random_bytes, 32)
                assert len(data) == 32
        finally:
            await _call(source.close)


class TestProfileBackedSource:
    """qr-sampler drawing from a PROFILE id (S10): the new SourceRouter
    path must uphold the same seam invariants as plain devices —
    sequence_id echo (=> echo_verified) and never-empty data."""

    async def _profile_key(self, profile_server, source_id: str) -> str:
        raw_key, _ = await profile_server.db.create_api_key(
            name=f"seam-{source_id}", primary_device_id=source_id
        )
        return raw_key

    async def test_unary_fetch_from_xnor_profile(self, profile_server, profile_address):
        key = await self._profile_key(profile_server, "qq-mock")
        source = QuantumGrpcSource(
            QRSamplerConfig(
                grpc_server_address=profile_address, grpc_api_key=key, fallback_mode="error"
            )
        )
        try:
            data = await _call(source.get_random_bytes, 256)
            assert len(data) == 256  # non-empty data through the profile path
        finally:
            await _call(source.close)

    async def test_pipelined_prefetch_echo_verified_via_profile(
        self, profile_server, profile_address
    ):
        key = await self._profile_key(profile_server, "pp-match")
        source = QuantumGrpcSource(
            QRSamplerConfig(
                grpc_server_address=profile_address, grpc_api_key=key, fallback_mode="error"
            )
        )
        try:
            await _call(source.warmup)
            ticket = await _call(source.prefetch, 64, 0x0123456789ABCDEF)
            assert ticket is not None
            data = await _call(source.get_random_bytes_with_ticket, 64, ticket)
            assert len(data) == 64
            assert ticket.echo_verified is True  # nonce echo survives the router
        finally:
            await _call(source.close)


class TestLegacyMethodPath:
    """The public QuantumRNG protocol still serves qr-sampler's generic
    unary client (documented field collision: sequence_id slot carries
    the µs timestamp, so echo verification stays off — by design)."""

    async def test_unary_fetch_on_qrng_path(self, address):
        source = _source(
            address,
            grpc_method_path="/qrng.QuantumRNG/GetRandomBytes",
            grpc_stream_method_path="",
        )
        try:
            data = await _call(source.get_random_bytes, 128)
            assert len(data) == 128
        finally:
            await _call(source.close)

    async def test_qrng_path_never_echo_verifies(self, address):
        source = _source(
            address,
            grpc_method_path="/qrng.QuantumRNG/GetRandomBytes",
            grpc_stream_method_path="",
        )
        try:
            await _call(source.warmup)
            ticket = await _call(source.prefetch, 16, 0x0123456789ABCDEF)
            assert ticket is not None
            data = await _call(source.get_random_bytes_with_ticket, 16, ticket)
            assert len(data) == 16
            assert ticket.echo_verified is False  # field 2 is a timestamp there
        finally:
            await _call(source.close)


class TestConditioningSeam:
    """qr-sampler asks for hash_truncate; Qbert0G applies, echoes, records.

    The two repos each carry their half of the contract (metadata keys,
    canonical echo string, the truncation rules); this is the only test
    where the real client meets the real server.
    """

    @pytest.fixture
    async def cond_address(self, tmp_path):
        from conftest import SEED_A, make_config

        from qbert0g.server import QbertServer

        config = make_config(
            tmp_path,
            controls=[{"id": "prng-a", "type": "prng_uniform", "seed": SEED_A}],
            conditioning={"allow_request": True},
        )
        srv = QbertServer(config)
        await srv.start()
        raw_key, _ = await srv.db.create_api_key(name="ctl", primary_device_id="prng-a")
        try:
            yield f"127.0.0.1:{srv.port}", raw_key, config
        finally:
            await srv.stop(grace=0.5)

    @pytest.mark.parametrize("mode", ["unary", "bidi_streaming"])
    async def test_hashed_bytes_replay_from_provenance(self, cond_address, mode):
        import json
        from pathlib import Path

        from qr_sampler.config.conditioning import Conditioning

        from qbert0g.controls import uniform_stream_bytes
        from qbert0g.profiles import hash_truncate

        address, raw_key, config = cond_address
        selection = Conditioning("hash_truncate", "sha256", 128, 16)
        source = QuantumGrpcSource(
            QRSamplerConfig(
                grpc_server_address=address,
                grpc_api_key=raw_key,
                grpc_mode=mode,
                fallback_mode="error",
            )
        )
        try:
            data = await _call(source.get_random_bytes, 1000, selection)
        finally:
            await _call(source.close)
        assert len(data) == 1000

        record = json.loads(Path(config.provenance.path).read_text().splitlines()[-1])
        fact, cond = record["inputs"][0], record["conditioning"]
        assert fact["kind"] == "prng"
        assert fact["raw_bytes"] == selection.raw_bytes_needed(1000) == 8064
        raw = uniform_stream_bytes(
            int(fact["seed"], 16), fact["stream_offset_bytes"], fact["raw_bytes"]
        )
        assert hash_truncate(raw, 1000, cond["hash"], cond["in_bytes"], cond["keep_bytes"]) == data

    async def test_server_with_conditioning_off_never_yields_raw_as_hashed(self, address):
        """Default server config refuses; the client surfaces unavailable."""
        from qr_sampler.config.conditioning import Conditioning
        from qr_sampler.exceptions import EntropyUnavailableError

        source = _source(address, grpc_retry_count=0)
        try:
            with pytest.raises(EntropyUnavailableError):
                await _call(
                    source.get_random_bytes, 64, Conditioning("hash_truncate", "sha256", 128, 16)
                )
            assert len(await _call(source.get_random_bytes, 64)) == 64
        finally:
            await _call(source.close)
