"""Shared fixtures: a fully wired server on an ephemeral loopback port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from qbert0g.config import Config
from qbert0g.server import QbertServer

ADMIN_KEY = "test-admin-key-0123456789abcdef"


def make_config(tmp_path: Path, **overrides) -> Config:
    """A validated config with one mock device and an ephemeral port."""
    data = {
        "server": {"listen": "127.0.0.1:0"},
        "database": {"path": str(tmp_path / "test.db")},
        "auth": {"api_key": ADMIN_KEY},
        "limits": {
            "max_bytes_per_request": 16384,
            "max_bytes_per_day": 10_000_000,
            "rate_limit_per_minute": 10_000,
        },
        "devices": [{"id": "mock-0", "type": "mock"}],
        "provenance": {"path": str(tmp_path / "provenance.jsonl")},
    }
    data.update(overrides)
    return Config.from_dict(data)


@pytest_asyncio.fixture
async def server(tmp_path: Path) -> AsyncIterator[QbertServer]:
    """A running QbertServer with a mock device and a bootstrap admin key."""
    srv = QbertServer(make_config(tmp_path))
    await srv.start()
    assert srv.port  # bound to a real ephemeral port
    try:
        yield srv
    finally:
        await srv.stop(grace=0.5)


@pytest.fixture
def address(server: QbertServer) -> str:
    return f"127.0.0.1:{server.port}"


# Two seeds for control fixtures; any 128-bit hex literals work.
SEED_A = "0x9e3779b97f4a7c15f39cc0605cedc834"
SEED_B = "0x243f6a8885a308d313198a2e03707344"


def make_profile_config(tmp_path: Path, **overrides) -> Config:
    """A config exposing the full source namespace: devices + controls + profiles."""
    data = {
        "devices": [{"id": "mock-0", "type": "mock"}, {"id": "mock-1", "type": "mock"}],
        "controls": [
            {"id": "prng-a", "type": "prng_uniform", "seed": SEED_A},
            {"id": "prng-b", "type": "prng_uniform", "seed": SEED_B},
        ],
        "profiles": [
            {"id": "qq-mock", "transform": "xnor", "inputs": ["mock-0", "mock-1"]},
            {"id": "pp-match", "transform": "xnor", "inputs": ["prng-a", "prng-b"]},
            {"id": "raw-mock", "transform": "identity", "inputs": ["mock-0"]},
        ],
    }
    data.update(overrides)
    return make_config(tmp_path, **data)


@pytest_asyncio.fixture
async def profile_server(tmp_path: Path) -> AsyncIterator[QbertServer]:
    """A running QbertServer whose namespace includes controls and profiles."""
    srv = QbertServer(make_profile_config(tmp_path))
    await srv.start()
    assert srv.port
    try:
        yield srv
    finally:
        await srv.stop(grace=0.5)


@pytest.fixture
def profile_address(profile_server: QbertServer) -> str:
    return f"127.0.0.1:{profile_server.port}"
