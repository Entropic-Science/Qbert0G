"""Configuration for Qbert0G.

One YAML file, one :class:`Config` object, validated loudly at load time:

- Unknown keys are an error (a typo must never silently change what kind
  of randomness is served).
- ``freshness.allow_pooling`` / ``allow_pregeneration`` must be ``false``
  — the server has no pooling to enable; the keys exist so the freshness
  contract is auditable from the config file alone.
- ``post_processing.mode`` is a named mode (``raw`` / ``sha256`` /
  ``raw_samples``), mapped to the qcc-cli ``-P`` integer at device init.

No global singleton: callers load a ``Config`` and pass it down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: post_processing.mode name -> qcc-cli ``-P`` integer.
POST_PROCESSING_MODES = {"sha256": 0, "raw": 1, "raw_samples": 2}

#: Device types driven through pyqcc's qcc serial protocol.
QCC_DEVICE_TYPES = frozenset({"firefly", "qcicada", "dragonfly"})

#: All accepted device types (``mock`` is software-only, NOT quantum).
DEVICE_TYPES = QCC_DEVICE_TYPES | {"mock"}

#: Per-request one-shot read ceiling by device type (bytes). Larger
#: requests fall back to a temporary continuous-mode burst.
ONE_SHOT_LIMITS = {"firefly": 35_200, "dragonfly": 35_200, "qcicada": 13_440}


class ConfigError(ValueError):
    """Raised for any invalid or unknown configuration value."""


def _check_keys(section: str, data: dict, allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"{section}: unknown key(s) {sorted(unknown)} (allowed: {sorted(allowed)})"
        )


@dataclass
class ServerConfig:
    """Bind addresses and transport-level limits."""

    listen: str = "127.0.0.1:50051"  # host:port TCP bind; "" disables TCP
    unix_socket: str = ""  # UDS path (preferred on-box transport); "" disables
    request_timeout: float = 5.0  # seconds to wait for an available device
    max_message_size: int = 16_777_216  # 16 MB
    failover_enabled: bool = True


@dataclass
class AuthConfig:
    """API-key transport settings + optional bootstrap admin key.

    ``api_key`` seeds the SQLite key store with an admin key on first
    startup (device ``*``); further keys are managed with ``qbert0g keys``.
    """

    api_key: str = ""
    header: str = "api-key"


@dataclass
class LimitsConfig:
    """Service-wide default caps (overridable per API key)."""

    max_bytes_per_request: int = 16_384
    max_bytes_per_day: int = 104_857_600  # 100 MB
    rate_limit_per_minute: int = 200


@dataclass
class FreshnessConfig:
    """The just-in-time measurement contract.

    ``allow_pooling`` / ``allow_pregeneration`` are declarative guards:
    the server has no pooling — setting either to ``true`` is refused.
    """

    flush_device_buffer: bool = True  # flush serial RX buffer before EVERY read
    emit_generation_timestamp: bool = True  # stamp responses with measurement time
    allow_pooling: bool = False
    allow_pregeneration: bool = False


@dataclass
class DeviceConfig:
    """One QRNG device (or a loudly-labelled mock)."""

    id: str
    type: str  # firefly | qcicada | dragonfly | mock
    path: str = ""  # serial device path; unused for mock
    baud_rate: int = 115_200
    timeout: float = 5.0
    enabled: bool = True
    streaming_mode: bool = False  # keep device in continuous mode permanently
    streaming_idle_timeout: float = 0.0  # minutes idle before sleeping (0 = never)
    rate_mbit_s: float | None = None  # informational (capacity notes)
    post_processing: str | None = None  # per-device override of the global mode


@dataclass
class Config:
    """Root configuration object."""

    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    post_processing_mode: str = "raw"
    devices: list[DeviceConfig] = field(default_factory=list)
    database_path: str = "./qbert0g.db"

    def qcc_mode_for(self, device: DeviceConfig) -> int:
        """The qcc-cli ``-P`` integer for *device* (override or global)."""
        mode = device.post_processing or self.post_processing_mode
        return POST_PROCESSING_MODES[mode]

    @classmethod
    def load(cls, config_path: str | os.PathLike[str] | None = None) -> Config:
        """Load and validate the YAML config file.

        Resolution: explicit *config_path* > ``QBERT0G_CONFIG`` env var >
        ``./config.yaml``. A missing file is an error — an entropy daemon
        must never start on silent defaults.
        """
        path = Path(config_path or os.environ.get("QBERT0G_CONFIG", "config.yaml"))
        if not path.exists():
            raise ConfigError(
                f"Config file not found: {path} "
                "(pass --config, set QBERT0G_CONFIG, or create ./config.yaml)"
            )
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        """Build a validated :class:`Config` from a parsed YAML dict."""
        _check_keys(
            "config",
            data,
            {"server", "auth", "limits", "freshness", "post_processing", "devices", "database"},
        )

        srv = data.get("server", {}) or {}
        _check_keys(
            "server",
            srv,
            {"listen", "unix_socket", "request_timeout", "max_message_size", "failover_enabled"},
        )
        server = ServerConfig(
            listen=str(srv.get("listen", "127.0.0.1:50051") or ""),
            unix_socket=str(srv.get("unix_socket", "") or ""),
            request_timeout=float(srv.get("request_timeout", 5.0)),
            max_message_size=int(srv.get("max_message_size", 16_777_216)),
            failover_enabled=bool(srv.get("failover_enabled", True)),
        )
        if not server.listen and not server.unix_socket:
            raise ConfigError("server: at least one of `listen` / `unix_socket` must be set")

        auth_data = data.get("auth", {}) or {}
        _check_keys("auth", auth_data, {"api_key", "header"})
        auth = AuthConfig(
            api_key=str(auth_data.get("api_key", "") or ""),
            header=str(auth_data.get("header", "api-key")),
        )

        lim = data.get("limits", {}) or {}
        _check_keys(
            "limits", lim, {"max_bytes_per_request", "max_bytes_per_day", "rate_limit_per_minute"}
        )
        limits = LimitsConfig(
            max_bytes_per_request=int(lim.get("max_bytes_per_request", 16_384)),
            max_bytes_per_day=int(lim.get("max_bytes_per_day", 104_857_600)),
            rate_limit_per_minute=int(lim.get("rate_limit_per_minute", 200)),
        )

        fresh = data.get("freshness", {}) or {}
        _check_keys(
            "freshness",
            fresh,
            {
                "flush_device_buffer",
                "emit_generation_timestamp",
                "allow_pooling",
                "allow_pregeneration",
            },
        )
        freshness = FreshnessConfig(
            flush_device_buffer=bool(fresh.get("flush_device_buffer", True)),
            emit_generation_timestamp=bool(fresh.get("emit_generation_timestamp", True)),
            allow_pooling=bool(fresh.get("allow_pooling", False)),
            allow_pregeneration=bool(fresh.get("allow_pregeneration", False)),
        )
        if freshness.allow_pooling or freshness.allow_pregeneration:
            raise ConfigError(
                "freshness: pooling/pre-generation is not implemented and never will be "
                "served by this daemon — allow_pooling and allow_pregeneration must be false"
            )

        pp = data.get("post_processing", {}) or {}
        _check_keys("post_processing", pp, {"mode"})
        pp_mode = str(pp.get("mode", "raw"))
        if pp_mode not in POST_PROCESSING_MODES:
            raise ConfigError(
                f"post_processing.mode must be one of {sorted(POST_PROCESSING_MODES)}, "
                f"got {pp_mode!r}"
            )

        devices: list[DeviceConfig] = []
        seen_ids: set[str] = set()
        for dev in data.get("devices", []) or []:
            _check_keys(
                f"devices[{dev.get('id', '?')}]",
                dev,
                {
                    "id",
                    "type",
                    "path",
                    "baud_rate",
                    "timeout",
                    "enabled",
                    "streaming_mode",
                    "streaming_idle_timeout",
                    "rate_mbit_s",
                    "post_processing",
                },
            )
            if "id" not in dev or "type" not in dev:
                raise ConfigError("devices: every device needs `id` and `type`")
            if dev["type"] not in DEVICE_TYPES:
                raise ConfigError(
                    f"devices[{dev['id']}]: type must be one of {sorted(DEVICE_TYPES)}, "
                    f"got {dev['type']!r}"
                )
            if dev["type"] != "mock" and not dev.get("path"):
                raise ConfigError(f"devices[{dev['id']}]: `path` is required for hardware devices")
            if dev["id"] in seen_ids:
                raise ConfigError(f"devices: duplicate id {dev['id']!r}")
            seen_ids.add(dev["id"])
            override = dev.get("post_processing")
            if override is not None and override not in POST_PROCESSING_MODES:
                raise ConfigError(
                    f"devices[{dev['id']}]: post_processing must be one of "
                    f"{sorted(POST_PROCESSING_MODES)}, got {override!r}"
                )
            devices.append(
                DeviceConfig(
                    id=str(dev["id"]),
                    type=str(dev["type"]),
                    path=str(dev.get("path", "")),
                    baud_rate=int(dev.get("baud_rate", 115_200)),
                    timeout=float(dev.get("timeout", 5.0)),
                    enabled=bool(dev.get("enabled", True)),
                    streaming_mode=bool(dev.get("streaming_mode", False)),
                    streaming_idle_timeout=float(dev.get("streaming_idle_timeout", 0.0)),
                    rate_mbit_s=(
                        float(dev["rate_mbit_s"]) if dev.get("rate_mbit_s") is not None else None
                    ),
                    post_processing=override,
                )
            )

        db = data.get("database", {}) or {}
        _check_keys("database", db, {"path"})

        return cls(
            server=server,
            auth=auth,
            limits=limits,
            freshness=freshness,
            post_processing_mode=pp_mode,
            devices=devices,
            database_path=str(db.get("path", "./qbert0g.db")),
        )
