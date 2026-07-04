"""End-to-end tests of ``qr_purity.PurityService`` over loopback (mock device).

Same conventions as test_server.py: ``grpc.aio`` client stubs against a
fully wired :class:`QbertServer` on an ephemeral port. Statistical
assertions (KS uniformity, the sensitivity pin) run on synthetic /
mock data at the transform level — never entropy thresholds on live
served output.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

import grpc
import numpy as np
import pytest
import pytest_asyncio
from conftest import ADMIN_KEY, make_config

import qbert0g
from qbert0g.coherence import CoherenceValue
from qbert0g.config import DeviceConfig
from qbert0g.devices import DeviceState, DeviceStatus
from qbert0g.fingerprint import Fingerprint
from qbert0g.integrators import integrate
from qbert0g.proto import purity_service_pb2, purity_service_pb2_grpc
from qbert0g.purity import EntropyLabel, Integrity, Origin, Processing
from qbert0g.server import QbertServer, health_clean, quantum_verified

METADATA = (("api-key", ADMIN_KEY),)

#: sha256 of purity_service.proto with line endings normalized to LF.
#: The SAME constant is pinned in qr-sampler's tests — two matching pins
#: enforce byte-identity of the wire contract without cross-repo file
#: access. Normalization keeps the pin stable across git autocrlf
#: checkouts; any semantic edit to the proto changes it.
PROTO_SHA256 = "738af813b298c5ee8f161afb0e69cfe44f12e0dbb9fe0d3cfc754e05574c0bc3"

#: A neff-consistent synthetic fingerprint matching the mock device
#: (os.urandom): unbiased bits, no serial correlation.
_FP_JSON = {
    "device_id": "mock",
    "firmware": "none",
    "ones_fraction": 0.5,
    "byte_mean": 127.5,
    "byte_std": 73.9,
    "bit_acf": {"1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0, "8": 0.0},
    "neff_factor": 1.0,
    "quantum_fraction_tier": "unrated",
    "source_dumps_sha256": ["ab" * 32],
    "fitted_utc": "2026-07-04T00:00:00+00:00",
    "session_ref": "synthetic-test-fixture",
}

SYNTH_FP = Fingerprint(
    device_id="synth",
    firmware="none",
    ones_fraction=0.5,
    byte_mean=127.5,
    byte_std=73.9,
    bit_acf={1: 0.0},
    neff_factor=1.0,
    quantum_fraction_tier="unrated",
    source_dumps_sha256=(),
    fitted_utc="2026-07-04T00:00:00+00:00",
    session_ref="synthetic-test-fixture",
)


def write_fingerprint(tmp_path: Path, device_id: str) -> str:
    data = dict(_FP_JSON, device_id=device_id)
    path = tmp_path / f"fp-{device_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def make_draw_config(tmp_path: Path, **overrides):
    """A config with two fingerprinted mock devices; mock-0 drawable."""
    data = {
        "devices": [
            {"id": "mock-0", "type": "mock", "fingerprint": write_fingerprint(tmp_path, "mock-0")},
            {"id": "mock-1", "type": "mock", "fingerprint": write_fingerprint(tmp_path, "mock-1")},
        ],
        "limits": {
            "max_bytes_per_request": 4_194_304,
            "max_bytes_per_day": 500_000_000,
            "rate_limit_per_minute": 10_000,
        },
        "integration": {"block_bytes": 4096, "sources": ["mock-0"], "secondaries": ["cusum"]},
    }
    data.update(overrides)
    return make_config(tmp_path, **data)


@pytest_asyncio.fixture
async def draw_server(tmp_path: Path):
    srv = QbertServer(make_draw_config(tmp_path))
    await srv.start()
    assert srv.port
    try:
        yield srv
    finally:
        await srv.stop(grace=0.5)


@pytest.fixture
def draw_address(draw_server: QbertServer) -> str:
    return f"127.0.0.1:{draw_server.port}"


def _stub(channel):
    return purity_service_pb2_grpc.PurityServiceStub(channel)


class TestProtoPin:
    def test_proto_sha256_pinned(self):
        path = Path(qbert0g.__file__).parent / "proto" / "purity_service.proto"
        raw = path.read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(raw).hexdigest() == PROTO_SHA256


class TestGetDraw:
    async def test_echo_shape_and_nondegenerate_u(self, draw_address):
        nonce = 0x7FEDCBA987654321  # a 63-bit commitment nonce
        async with grpc.aio.insecure_channel(draw_address) as channel:
            reply = await _stub(channel).GetDraw(
                purity_service_pb2.DrawRequest(sequence_id=nonce), metadata=METADATA
            )
        assert reply.sequence_id == nonce  # echoed verbatim
        assert 0.0 < reply.u < 1.0  # clamped; never degenerate
        assert math.isfinite(reply.z)
        assert reply.source_id == "mock-0"
        assert reply.integrator == "bit_z"
        assert reply.integrated_bytes == 4096
        assert abs(reply.generation_timestamp_ns / 1e9 - time.time()) < 60
        assert reply.coherence_valid is False  # coherence disabled here

    async def test_purity_label_per_request_bits(self, draw_address):
        async with grpc.aio.insecure_channel(draw_address) as channel:
            reply = await _stub(channel).GetDraw(
                purity_service_pb2.DrawRequest(), metadata=METADATA
            )
        label = EntropyLabel.from_canonical(reply.purity_label)  # exact round-trip
        assert label.origin is Origin.TRUE_RANDOM  # mock is loudly NOT quantum
        assert label.integrity is Integrity.INTACT
        assert label.processing is Processing.RAW
        assert label.amplified is True
        assert label.integration_n == 4096
        assert label.quantum_verified is False  # origin gate: never QV on a mock
        assert "/QV" not in reply.purity_label

    async def test_default_block_bytes_is_two_mib(self, tmp_path):
        """block_bytes=0 in the request means integration.block_bytes (2 MiB)."""
        srv = QbertServer(
            make_draw_config(tmp_path, integration={"sources": ["mock-0"]})
        )
        await srv.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{srv.port}") as channel:
                reply = await _stub(channel).GetDraw(
                    purity_service_pb2.DrawRequest(block_bytes=0), metadata=METADATA
                )
            assert reply.integrated_bytes == 2_097_152
            assert 0.0 < reply.u < 1.0
        finally:
            await srv.stop(grace=0.5)

    async def test_explicit_source_id(self, draw_address):
        async with grpc.aio.insecure_channel(draw_address) as channel:
            reply = await _stub(channel).GetDraw(
                purity_service_pb2.DrawRequest(source_id="mock-0"), metadata=METADATA
            )
        assert reply.source_id == "mock-0"

    async def test_undrawable_source_invalid(self, draw_address):
        # mock-1 is configured and fingerprinted but NOT in integration.sources
        async with grpc.aio.insecure_channel(draw_address) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await _stub(channel).GetDraw(
                    purity_service_pb2.DrawRequest(source_id="mock-1"), metadata=METADATA
                )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_unknown_source_invalid(self, draw_address):
        async with grpc.aio.insecure_channel(draw_address) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await _stub(channel).GetDraw(
                    purity_service_pb2.DrawRequest(source_id="nope"), metadata=METADATA
                )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_unconfigured_service_failed_precondition(self, address):
        """The base server fixture has no integration.sources — no draws."""
        async with grpc.aio.insecure_channel(address) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await _stub(channel).GetDraw(
                    purity_service_pb2.DrawRequest(), metadata=METADATA
                )
        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION


class TestRequestGateReuse:
    """Auth, limits and usage behave identically on the third protocol."""

    async def test_missing_api_key_unauthenticated(self, draw_address):
        async with grpc.aio.insecure_channel(draw_address) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await _stub(channel).GetDraw(purity_service_pb2.DrawRequest())
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_invalid_api_key_unauthenticated(self, draw_address):
        async with grpc.aio.insecure_channel(draw_address) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await _stub(channel).GetDraw(
                    purity_service_pb2.DrawRequest(), metadata=(("api-key", "wrong"),)
                )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_block_over_per_request_cap_invalid(self, draw_address):
        async with grpc.aio.insecure_channel(draw_address) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await _stub(channel).GetDraw(
                    purity_service_pb2.DrawRequest(block_bytes=8_388_608),
                    metadata=METADATA,
                )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_usage_accounts_block_bytes(self, draw_server, draw_address):
        """block_bytes is the accounted quantity, in the SHARED ledger."""
        async with grpc.aio.insecure_channel(draw_address) as channel:
            await _stub(channel).GetDraw(
                purity_service_pb2.DrawRequest(block_bytes=4096), metadata=METADATA
            )
        key = await draw_server.db.validate_api_key(ADMIN_KEY)
        usage = await draw_server.db.get_usage_today(key["id"])
        assert usage["bytes_served"] == 4096
        assert usage["requests"] == 1


class TestProvenance:
    async def test_exactly_one_record_with_draw_extras(self, draw_server, draw_address):
        nonce = 424242
        prov_path = Path(draw_server.config.provenance.path)
        before = len(prov_path.read_text().splitlines()) if prov_path.exists() else 0
        async with grpc.aio.insecure_channel(draw_address) as channel:
            reply = await _stub(channel).GetDraw(
                purity_service_pb2.DrawRequest(sequence_id=nonce, block_bytes=4096),
                metadata=METADATA,
            )
        lines = prov_path.read_text().splitlines()
        assert len(lines) == before + 1  # exactly one record per request
        record = json.loads(lines[-1])
        assert record["protocol"] == "qr_purity"
        assert record["sequence_id"] == nonce
        assert record["source_id"] == "mock-0"
        assert record["served_bytes"] == 4096
        assert record["integrator"] == "bit_z"
        assert record["integrated_bytes"] == 4096
        assert record["z"] == pytest.approx(reply.z)
        assert "cusum" in record["secondaries"]
        assert record["purity_label"] == reply.purity_label
        assert record["coherence"] is None  # coherence disabled: null, not zeros
        # the pinned baseline: sha256 of the exact fingerprint file in force
        fp_path = Path(draw_server.config.devices[0].fingerprint)
        assert record["fingerprint_sha256"] == hashlib.sha256(fp_path.read_bytes()).hexdigest()


class TestCoherencePaths:
    """coherence_valid=False whenever the value is absent/stale — never faked."""

    def _coherence_overrides(self):
        return {
            "coherence": {
                "enabled": True,
                "pair": ["mock-0", "mock-1"],
                "refresh_s": 3600.0,
                "max_age_s": 5.0,
            }
        }

    async def _draw(self, port):
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            return await _stub(channel).GetDraw(
                purity_service_pb2.DrawRequest(block_bytes=1024), metadata=METADATA
            )

    @pytest_asyncio.fixture
    async def coherence_server(self, tmp_path):
        srv = QbertServer(make_draw_config(tmp_path, **self._coherence_overrides()))
        await srv.start()
        assert srv.coherence_monitor is not None  # started when enabled
        # Deterministic tests: stop the background loop, inject values.
        await srv.coherence_monitor.stop()
        try:
            yield srv
        finally:
            await srv.stop(grace=0.5)

    def _value(self, age_s: float) -> CoherenceValue:
        return CoherenceValue(
            r=0.5,
            lag=1,
            z_c=4.2,
            k_eff=32,
            max_pair_skew_ns=0,
            computed_monotonic_ns=time.monotonic_ns() - int(age_s * 1e9),
        )

    async def test_never_computed_is_invalid(self, coherence_server):
        coherence_server.coherence_monitor._latest = None
        reply = await self._draw(coherence_server.port)
        assert reply.coherence_valid is False
        assert reply.coherence_z == 0.0
        assert reply.coherence_r == 0.0

    async def test_stale_value_is_invalid_but_reported(self, coherence_server):
        coherence_server.coherence_monitor._latest = self._value(age_s=10.0)
        reply = await self._draw(coherence_server.port)
        assert reply.coherence_valid is False  # older than max_age_s=5
        assert reply.coherence_z == pytest.approx(4.2)  # last value, honestly flagged

    async def test_fresh_value_is_valid(self, coherence_server):
        coherence_server.coherence_monitor._latest = self._value(age_s=0.0)
        reply = await self._draw(coherence_server.port)
        assert reply.coherence_valid is True
        assert reply.coherence_z == pytest.approx(4.2)
        assert reply.coherence_r == pytest.approx(0.5)

    async def test_disabled_monitor_never_starts(self, draw_server, draw_address):
        assert draw_server.coherence_monitor is None
        async with grpc.aio.insecure_channel(draw_address) as channel:
            reply = await _stub(channel).GetDraw(
                purity_service_pb2.DrawRequest(), metadata=METADATA
            )
        assert reply.coherence_valid is False


class TestQuantumVerified:
    """The per-request QV bit: health snapshot + fingerprint tolerance."""

    QUANTUM_INTACT = EntropyLabel(
        origin=Origin.QUANTUM,
        integrity=Integrity.INTACT,
        processing=Processing.RAW,
        expanded=False,
        quantum_fraction_tier="unrated",
    )

    def _chardev_state(self, *, status=DeviceStatus.ONLINE, error_present="0") -> DeviceState:
        config = DeviceConfig(id="df-0", type="chardev", path="/dev/qrngDF0")
        state = DeviceState(config=config, status=status)
        state.last_error_present = error_present
        return state

    def test_clean_healthy_in_tolerance_is_verified(self):
        assert quantum_verified(self.QUANTUM_INTACT, self._chardev_state(), 1.0, 6.0) is True

    def test_dirty_health_snapshot_flips_off(self):
        state = self._chardev_state(error_present="1")
        assert quantum_verified(self.QUANTUM_INTACT, state, 1.0, 6.0) is False

    def test_fingerprint_tolerance_breach_flips_off(self):
        assert quantum_verified(self.QUANTUM_INTACT, self._chardev_state(), 6.5, 6.0) is False
        assert quantum_verified(self.QUANTUM_INTACT, self._chardev_state(), -6.5, 6.0) is False

    def test_offline_device_flips_off(self):
        state = self._chardev_state(status=DeviceStatus.ERROR)
        assert quantum_verified(self.QUANTUM_INTACT, state, 1.0, 6.0) is False

    def test_busy_device_is_still_healthy(self):
        state = self._chardev_state(status=DeviceStatus.BUSY)
        assert quantum_verified(self.QUANTUM_INTACT, state, 1.0, 6.0) is True

    def test_non_quantum_origin_never_verified(self):
        label = EntropyLabel(
            origin=Origin.TRUE_RANDOM,
            integrity=Integrity.INTACT,
            processing=Processing.RAW,
            expanded=False,
            quantum_fraction_tier="unrated",
        )
        assert quantum_verified(label, self._chardev_state(), 1.0, 6.0) is False

    def test_scrambled_integrity_never_verified(self):
        label = EntropyLabel(
            origin=Origin.QUANTUM,
            integrity=Integrity.SCRAMBLED,
            processing=Processing.UNIFORM,
            expanded=False,
            quantum_fraction_tier="unrated",
        )
        assert quantum_verified(label, self._chardev_state(), 1.0, 6.0) is False

    def test_no_device_state_is_not_health_checkable(self):
        assert health_clean(None) is False
        assert quantum_verified(self.QUANTUM_INTACT, None, 1.0, 6.0) is False

    def test_non_chardev_health_ignores_error_fields(self):
        config = DeviceConfig(id="ff-0", type="firefly", path="COM3")
        state = DeviceState(config=config, status=DeviceStatus.ONLINE)
        assert health_clean(state) is True


class TestStreamDraws:
    async def test_bidi_correlates_by_echo(self, draw_address):
        nonces = [101, 202, 303]
        async with grpc.aio.insecure_channel(draw_address) as channel:
            call = _stub(channel).StreamDraws(metadata=METADATA)
            replies = []
            for nonce in nonces:
                await call.write(
                    purity_service_pb2.DrawRequest(sequence_id=nonce, block_bytes=1024)
                )
                replies.append(await call.read())
            await call.done_writing()
        assert [r.sequence_id for r in replies] == nonces
        assert all(0.0 < r.u < 1.0 for r in replies)

    def test_u_is_ks_uniform_over_2000_mock_draws(self):
        """KS check on the served statistic over a SEEDED mock (spec §7.12).

        Transform-level on synthetic data — the repo's "no entropy
        thresholds on served output" philosophy allows exactly this
        shape. 2000 draws of 512-byte blocks through the same
        ``integrate`` call the servicer makes; alpha = 0.01:
        D_crit = 1.628 / sqrt(n) (asymptotic Kolmogorov).
        """
        rng = np.random.default_rng(0x5EED)
        n = 2000
        us = [
            integrate("bit_z", rng.bytes(512), SYNTH_FP).u
            for _ in range(n)
        ]
        u_sorted = np.sort(np.asarray(us))
        ranks = np.arange(1, n + 1, dtype=np.float64)
        d = max(
            float(np.max(ranks / n - u_sorted)),
            float(np.max(u_sorted - (ranks - 1) / n)),
        )
        assert d < 1.628 / math.sqrt(n)


class TestSensitivityPin:
    def test_128kib_block_bias_pin(self):
        """Spec §7.11: at bias delta = 3e-3 (on the ±1 bit-walk mean scale,

        i.e. a ones-fraction offset of delta/2), a 128 KiB block with
        neff_factor = 1.0 gives E[z] = sqrt(8*131072) * delta /
        sqrt(f0*(1-f0)*4) ≈ 3.1 — pinned as mean z in [2, 4].
        """
        rng = np.random.default_rng(20260704)
        block_bytes = 131_072  # 128 KiB
        n_bits = 8 * block_bytes
        p_one = 0.5 + 3e-3 / 2.0
        zs = []
        for _ in range(16):
            bits = (rng.random(n_bits) < p_one).astype(np.uint8)
            raw = np.packbits(bits).tobytes()
            zs.append(integrate("bit_z", raw, SYNTH_FP).z)
        mean_z = float(np.mean(zs))
        assert 2.0 <= mean_z <= 4.0

    def test_unbiased_block_is_near_null(self):
        rng = np.random.default_rng(4)
        raw = rng.integers(0, 256, size=131_072, dtype=np.uint8).tobytes()
        result = integrate("bit_z", raw, SYNTH_FP)
        assert abs(result.z) < 5.0
        assert 0.0 < result.u < 1.0
