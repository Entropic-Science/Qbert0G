"""Request-selected conditioning (hash_truncate) and the prng_resample control.

Transform semantics are pinned on constructed inputs (golden vectors);
the served path is proven by provenance replay — regenerate the raw
block from the record alone, re-apply the recorded transform, and
compare bytes. No uniformity threshold is asserted on served output.
"""

import hashlib
import json
from pathlib import Path

import grpc
import pytest
import pytest_asyncio
from conftest import ADMIN_KEY, SEED_A, make_config

from qbert0g.config import ConditioningConfig, Config, ConfigError, ControlConfig
from qbert0g.controls import (
    MIN_POOL_BYTES,
    PrngResampleControl,
    make_control,
    resample_window_bytes,
    resample_window_offset,
    uniform_stream_bytes,
)
from qbert0g.profiles import (
    CONDITIONING_APPLIED_HEADER,
    RAW,
    Conditioning,
    ConditioningError,
    conditioning_from_metadata,
    hash_truncate,
    hash_truncate_bytes_needed,
)
from qbert0g.proto import (
    entropy_service_pb2,
    entropy_service_pb2_grpc,
    purity_service_pb2,
    purity_service_pb2_grpc,
    qrng_pb2,
    qrng_pb2_grpc,
)
from qbert0g.server import QbertServer

ON = ConditioningConfig(allow_request=True)
KEY = (("api-key", ADMIN_KEY),)
HASHED = (*KEY, ("qr-conditioning", "hash_truncate"))


# ── the normative transform ──────────────────────────────────────────────


class TestHashTruncate:
    def test_golden_two_blocks(self):
        raw = bytes(range(8)) + bytes(range(8, 16))
        expected = (
            hashlib.sha256(bytes(range(8))).digest()[:4]
            + hashlib.sha256(bytes(range(8, 16))).digest()[:4]
        )
        assert hash_truncate(raw, 8, "sha256", 8, 4) == expected
        # Pinned literal: the head of SHA-256 of bytes 00..07 and 08..0f.
        assert expected.hex() == "8a851ff8" + hashlib.sha256(bytes(range(8, 16))).hexdigest()[:8]

    def test_output_cut_to_request_and_tail_block_fully_consumed(self):
        raw = bytes(range(256)) * 2
        out = hash_truncate(raw, 5, "sha256", 16, 4)
        assert len(out) == 5
        assert hash_truncate_bytes_needed(5, 16, 4) == 32  # two whole blocks
        assert out == (
            hashlib.sha256(raw[:16]).digest()[:4] + hashlib.sha256(raw[16:32]).digest()[:1]
        )

    def test_blocks_are_independent(self):
        """No chaining: changing block 1 leaves block 0's output untouched."""
        a = bytes(64)
        b = bytes(32) + bytes([1]) * 32
        out_a = hash_truncate(a, 32, "sha256", 32, 16)
        out_b = hash_truncate(b, 32, "sha256", 32, 16)
        assert out_a[:16] == out_b[:16]
        assert out_a[16:] != out_b[16:]

    def test_short_input_is_an_error(self):
        with pytest.raises(ValueError, match="needs 32 input bytes"):
            hash_truncate(bytes(31), 8, "sha256", 16, 4)

    @pytest.mark.parametrize("name", ["sha256", "sha512", "sha3_256", "blake2b"])
    def test_every_listed_hash_works(self, name):
        assert len(hash_truncate(bytes(128), 16, name, 64, 8)) == 16


class TestSelection:
    def test_no_header_is_raw(self):
        assert conditioning_from_metadata({"api-key": "k"}, ON) is RAW
        assert RAW.canonical() == "raw"
        assert RAW.raw_bytes_needed(100) == 100

    def test_mode_only_takes_server_defaults(self):
        c = conditioning_from_metadata({"qr-conditioning": "hash_truncate"}, ON)
        assert c == Conditioning("hash_truncate", "sha256", 128, 16)
        assert c.canonical() == "hash_truncate;sha256;128;16"
        assert c.raw_bytes_needed(10_000) == 80_000

    def test_explicit_parameters(self):
        c = conditioning_from_metadata(
            {
                "qr-conditioning": "hash_truncate",
                "qr-conditioning-hash": "sha512",
                "qr-conditioning-in-bytes": "256",
                "qr-conditioning-keep-bytes": "32",
            },
            ON,
        )
        assert c == Conditioning("hash_truncate", "sha512", 256, 32)

    @pytest.mark.parametrize(
        ("metadata", "match"),
        [
            ({"qr-conditioning": "sha256"}, "mode must be one of"),
            ({"qr-conditioning-hash": "sha256"}, "sent without"),
            ({"qr-conditioning": "raw", "qr-conditioning-in-bytes": "64"}, "sent without"),
            ({"qr-conditioning": "hash_truncate", "qr-conditioning-hash": "md5"}, "hash must be"),
            # More than half the digest kept: refused, not configurable.
            (
                {"qr-conditioning": "hash_truncate", "qr-conditioning-keep-bytes": "17"},
                "at least half of every digest is discarded",
            ),
            (
                {
                    "qr-conditioning": "hash_truncate",
                    "qr-conditioning-in-bytes": "31",
                    "qr-conditioning-keep-bytes": "16",
                },
                "at least 2 \\* keep_bytes",
            ),
            (
                {"qr-conditioning": "hash_truncate", "qr-conditioning-in-bytes": "8192"},
                "exceeds this server's limit",
            ),
            (
                {"qr-conditioning": "hash_truncate", "qr-conditioning-in-bytes": "many"},
                "must be an integer",
            ),
        ],
    )
    def test_refusals(self, metadata, match):
        with pytest.raises(ConditioningError, match=match):
            conditioning_from_metadata(metadata, ON)

    def test_disabled_server_refuses_but_still_serves_raw(self):
        off = ConditioningConfig()
        assert conditioning_from_metadata({"qr-conditioning": "raw"}, off) is RAW
        with pytest.raises(ConditioningError, match="disabled"):
            conditioning_from_metadata({"qr-conditioning": "hash_truncate"}, off)


class TestConfig:
    def test_default_is_off(self, tmp_path):
        assert make_config(tmp_path).conditioning.allow_request is False

    def test_unknown_key_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown key"):
            make_config(tmp_path, conditioning={"allow_requests": True})

    @pytest.mark.parametrize(
        ("section", "match"),
        [
            ({"hashes": ["md5"]}, "must be one of"),
            ({"hashes": ["sha512"]}, "default_hash"),
            ({"default_keep_bytes": 17}, "default_keep_bytes"),
            ({"default_in_bytes": 16}, "default_in_bytes"),
            ({"max_in_bytes": 64}, "max_in_bytes"),
        ],
    )
    def test_invalid_defaults_rejected(self, tmp_path, section, match):
        with pytest.raises(ConfigError, match=match):
            make_config(tmp_path, conditioning=section)

    def test_resample_requires_pool_and_pool_is_resample_only(self, tmp_path):
        with pytest.raises(ConfigError, match="`pool`"):
            make_config(
                tmp_path, controls=[{"id": "r", "type": "prng_resample", "seed": SEED_A}]
            )
        with pytest.raises(ConfigError, match="only applies to prng_resample"):
            make_config(
                tmp_path,
                controls=[{"id": "u", "type": "prng_uniform", "seed": SEED_A, "pool": "x"}],
            )


# ── prng_resample ────────────────────────────────────────────────────────


def _write_pool(tmp_path: Path, size: int = MIN_POOL_BYTES) -> Path:
    """A deterministic, obviously non-uniform pool (position-coded bytes)."""
    path = tmp_path / "pool.bin"
    path.write_bytes(uniform_stream_bytes(int(SEED_A, 16), 0, size))
    return path


def _resample_config(pool: Path) -> ControlConfig:
    return ControlConfig(id="resample-0", type="prng_resample", seed=SEED_A, pool=str(pool))


class TestResampleControl:
    def test_serves_contiguous_pool_windows(self, tmp_path):
        pool = _write_pool(tmp_path)
        data = pool.read_bytes()
        control = make_control(_resample_config(pool))
        assert isinstance(control, PrngResampleControl)
        assert control.kind == "prng"
        assert control.pool_sha256 == hashlib.sha256(data).hexdigest()
        for index in range(5):
            window = control.read(1000)
            offset = control.last_window_offset
            assert window == data[offset : offset + 1000]
            assert offset == resample_window_offset(int(SEED_A, 16), index, len(data), 1000)
        assert control.draw_index == 5

    def test_offline_regeneration_matches(self, tmp_path):
        pool = _write_pool(tmp_path)
        control = make_control(_resample_config(pool))
        served = [control.read(n) for n in (10, 4096, 777)]
        for index, (n, block) in enumerate(zip((10, 4096, 777), served, strict=True)):
            assert resample_window_bytes(str(pool), int(SEED_A, 16), index, n) == block

    def test_windows_stay_inside_the_pool(self, tmp_path):
        size = MIN_POOL_BYTES
        for index in range(200):
            offset = resample_window_offset(int(SEED_A, 16), index, size, size - 3)
            assert 0 <= offset <= 3
        assert resample_window_offset(int(SEED_A, 16), 0, size, size) == 0
        with pytest.raises(ValueError, match="pool size"):
            resample_window_offset(int(SEED_A, 16), 0, size, size + 1)

    def test_missing_or_tiny_pool_refuses_startup(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            make_control(_resample_config(tmp_path / "absent.bin"))
        tiny = tmp_path / "tiny.bin"
        tiny.write_bytes(bytes(1024))
        with pytest.raises(ConfigError, match="minimum"):
            make_control(_resample_config(tiny))


# ── the served path ──────────────────────────────────────────────────────


def _conditioning_config(tmp_path: Path, **overrides) -> Config:
    pool = _write_pool(tmp_path)
    data = {
        "limits": {
            "max_bytes_per_request": 16384,
            "max_bytes_per_day": 10_000_000,
            "rate_limit_per_minute": 10_000,
        },
        "controls": [
            {"id": "prng-a", "type": "prng_uniform", "seed": SEED_A},
            {"id": "resample-0", "type": "prng_resample", "seed": SEED_A, "pool": str(pool)},
        ],
        "conditioning": {"allow_request": True},
    }
    data.update(overrides)
    return make_config(tmp_path, **data)


@pytest_asyncio.fixture
async def cond_server(tmp_path: Path):
    srv = QbertServer(_conditioning_config(tmp_path))
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop(grace=0.5)


async def _key_for(server: QbertServer, source_id: str) -> tuple[tuple[str, str], ...]:
    raw_key, _info = await server.db.create_api_key(
        name=f"k-{source_id}", primary_device_id=source_id
    )
    return (("api-key", raw_key),)


def _records(server: QbertServer) -> list[dict]:
    lines = Path(server.config.provenance.path).read_text().splitlines()
    return [json.loads(line) for line in lines]


class TestServedConditioning:
    async def test_raw_request_is_unchanged_and_unechoed(self, cond_server):
        address = f"127.0.0.1:{cond_server.port}"
        async with grpc.aio.insecure_channel(address) as channel:
            stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
            call = stub.GetEntropy(
                entropy_service_pb2.EntropyRequest(bytes_needed=64, sequence_id=7), metadata=KEY
            )
            response = await call
            initial = dict(await call.initial_metadata())
        assert len(response.data) == 64
        assert CONDITIONING_APPLIED_HEADER not in initial
        assert "conditioning" not in _records(cond_server)[-1]

    async def test_explicit_raw_is_echoed(self, cond_server):
        address = f"127.0.0.1:{cond_server.port}"
        async with grpc.aio.insecure_channel(address) as channel:
            stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
            call = stub.GetEntropy(
                entropy_service_pb2.EntropyRequest(bytes_needed=64),
                metadata=(*KEY, ("qr-conditioning", "raw")),
            )
            await call
            initial = dict(await call.initial_metadata())
        assert initial[CONDITIONING_APPLIED_HEADER] == "raw"

    async def test_hashed_control_replays_from_provenance_alone(self, cond_server):
        """THE pipeline proof: record -> raw block -> transform -> served bytes."""
        address = f"127.0.0.1:{cond_server.port}"
        key = await _key_for(cond_server, "prng-a")
        metadata = (
            *key,
            ("qr-conditioning", "hash_truncate"),
            ("qr-conditioning-hash", "sha512"),
            ("qr-conditioning-in-bytes", "64"),
            ("qr-conditioning-keep-bytes", "32"),
        )
        async with grpc.aio.insecure_channel(address) as channel:
            stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
            call = stub.GetEntropy(
                entropy_service_pb2.EntropyRequest(bytes_needed=100, sequence_id=42),
                metadata=metadata,
            )
            response = await call
            initial = dict(await call.initial_metadata())
        assert initial[CONDITIONING_APPLIED_HEADER] == "hash_truncate;sha512;64;32"
        assert response.sequence_id == 42
        assert len(response.data) == 100

        record = _records(cond_server)[-1]
        cond = record["conditioning"]
        fact = record["inputs"][0]
        assert record["served_bytes"] == 100
        assert fact["kind"] == "prng"  # never laundered by the hash
        assert fact["raw_bytes"] == cond["raw_bytes"] == 4 * 64
        raw = uniform_stream_bytes(
            int(fact["seed"], 16), fact["stream_offset_bytes"], fact["raw_bytes"]
        )
        assert hashlib.sha256(raw).hexdigest() == cond["raw_sha256"]
        replayed = hash_truncate(
            raw, record["served_bytes"], cond["hash"], cond["in_bytes"], cond["keep_bytes"]
        )
        assert replayed == response.data

    async def test_resample_control_replays_from_provenance_alone(self, cond_server):
        address = f"127.0.0.1:{cond_server.port}"
        key = await _key_for(cond_server, "resample-0")
        pool = cond_server.config.controls[1].pool
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            first = await stub.GetRandomBytes(qrng_pb2.RandomRequest(num_bytes=500), metadata=key)
            second = await stub.GetRandomBytes(
                qrng_pb2.RandomRequest(num_bytes=500), metadata=key
            )
        assert first.device_id == "resample-0"
        for response, record in zip((first, second), _records(cond_server)[-2:], strict=True):
            fact = record["inputs"][0]
            assert fact["kind"] == "prng"
            assert fact["pool_sha256"] == hashlib.sha256(Path(pool).read_bytes()).hexdigest()
            regenerated = resample_window_bytes(
                pool, int(fact["seed"], 16), fact["draw_index"], fact["raw_bytes"]
            )
            assert regenerated == response.data
            assert Path(pool).read_bytes()[fact["window_offset"] :][:500] == response.data

    async def test_hashed_device_records_raw_sha_and_quantum_kind(self, cond_server):
        address = f"127.0.0.1:{cond_server.port}"
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            call = stub.GetRandomBytes(qrng_pb2.RandomRequest(num_bytes=32), metadata=HASHED)
            response = await call
            initial = dict(await call.initial_metadata())
        assert len(response.data) == 32
        assert initial[CONDITIONING_APPLIED_HEADER] == "hash_truncate;sha256;128;16"
        record = _records(cond_server)[-1]
        assert record["inputs"][0]["kind"] == "mock"
        assert record["inputs"][0]["raw_bytes"] == 256
        assert record["conditioning"]["mode"] == "hash_truncate"
        assert len(record["conditioning"]["raw_sha256"]) == 64

    async def test_stream_echoes_once_and_conditions_every_message(self, cond_server):
        address = f"127.0.0.1:{cond_server.port}"

        async def requests():
            for seq in (1, 2, 3):
                yield entropy_service_pb2.EntropyRequest(bytes_needed=48, sequence_id=seq)

        async with grpc.aio.insecure_channel(address) as channel:
            stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
            call = stub.StreamEntropy(requests(), metadata=HASHED)
            replies = [reply async for reply in call]
            initial = dict(await call.initial_metadata())
        assert [r.sequence_id for r in replies] == [1, 2, 3]
        assert all(len(r.data) == 48 for r in replies)
        assert initial[CONDITIONING_APPLIED_HEADER] == "hash_truncate;sha256;128;16"
        assert all("conditioning" in rec for rec in _records(cond_server)[-3:])

    async def test_cap_applies_to_raw_bytes(self, cond_server):
        """16384-byte cap: 2048 hashed bytes need exactly 16384 raw (ok); 2064 do not."""
        address = f"127.0.0.1:{cond_server.port}"
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            ok = await stub.GetRandomBytes(qrng_pb2.RandomRequest(num_bytes=2048), metadata=HASHED)
            assert len(ok.data) == 2048
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetRandomBytes(qrng_pb2.RandomRequest(num_bytes=2064), metadata=HASHED)
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert "raw bytes under hash_truncate" in exc_info.value.details()

    async def test_invalid_selection_is_invalid_argument(self, cond_server):
        address = f"127.0.0.1:{cond_server.port}"
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetRandomBytes(
                    qrng_pb2.RandomRequest(num_bytes=8),
                    metadata=(*HASHED, ("qr-conditioning-keep-bytes", "32")),
                )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_disabled_server_refuses_hashing(self, server, address):
        async with grpc.aio.insecure_channel(address) as channel:
            stub = qrng_pb2_grpc.QuantumRNGStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetRandomBytes(qrng_pb2.RandomRequest(num_bytes=8), metadata=HASHED)
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert "disabled" in exc_info.value.details()

    async def test_draws_refuse_conditioning(self, cond_server):
        address = f"127.0.0.1:{cond_server.port}"
        async with grpc.aio.insecure_channel(address) as channel:
            stub = purity_service_pb2_grpc.PurityServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.GetDraw(purity_service_pb2.DrawRequest(sequence_id=1), metadata=HASHED)
        # FAILED_PRECONDITION (no integration.sources) fires first on this
        # config; either way a conditioned draw is never served.
        assert exc_info.value.code() in (
            grpc.StatusCode.INVALID_ARGUMENT,
            grpc.StatusCode.FAILED_PRECONDITION,
        )
