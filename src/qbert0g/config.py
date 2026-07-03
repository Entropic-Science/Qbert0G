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
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: post_processing.mode name -> qcc-cli ``-P`` integer.
POST_PROCESSING_MODES = {"sha256": 0, "raw": 1, "raw_samples": 2}

#: Device types driven through pyqcc's qcc serial protocol.
QCC_DEVICE_TYPES = frozenset({"firefly", "qcicada", "dragonfly"})

#: All accepted device types. ``mock`` is software-only, NOT quantum.
#: ``chardev`` is a PCIe Dragonfly exposed as a plain character device
#: (``/dev/qrngDF*``) — no pyqcc, no qcc-cli post-processing chain; it
#: serves whatever the driver DMA delivers.
DEVICE_TYPES = QCC_DEVICE_TYPES | {"mock", "chardev"}

#: Per-request one-shot read ceiling by device type (bytes). Larger
#: requests fall back to a temporary continuous-mode burst. ``chardev``
#: deliberately has no entry: DMA reads are effectively unbounded.
ONE_SHOT_LIMITS = {"firefly": 35_200, "dragonfly": 35_200, "qcicada": 13_440}

#: PRNG control source types. Both are seeded pseudorandom generators —
#: loudly NOT quantum (see controls.py). ``prng_markov`` additionally
#: needs an npz model file (fit with scripts/fit_markov.py).
CONTROL_TYPES = frozenset({"prng_uniform", "prng_markov"})

#: Profile transform name -> required input count. The transform
#: implementations live in profiles.py; the arity map lives HERE so
#: config validation and the transform library share one source of
#: truth without config.py importing anything internal.
TRANSFORM_ARITY = {"identity": 1, "xnor": 2, "parity": 1}

#: PRNG seeds are exactly 128 bits: ``0x`` + 32 hex digits. Required —
#: there is no silent time-seeding (a control must be regenerable).
_SEED_RE = re.compile(r"^0x[0-9a-fA-F]{32}$")


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
    type: str  # firefly | qcicada | dragonfly | chardev | mock
    path: str = ""  # serial device path; unused for mock
    baud_rate: int = 115_200
    timeout: float = 5.0
    enabled: bool = True
    streaming_mode: bool = False  # keep device in continuous mode permanently
    streaming_idle_timeout: float = 0.0  # minutes idle before sleeping (0 = never)
    rate_mbit_s: float | None = None  # informational (capacity notes)
    post_processing: str | None = None  # per-device override of the global mode
    pci_address: str | None = None  # chardev only; enables freshness flush + health


@dataclass
class ControlConfig:
    """One seeded PRNG control source — pseudorandom, NOT quantum.

    ``seed`` is a 128-bit hex literal (``0x`` + 32 digits), required so
    every served block is regenerable offline from
    ``(source_id, seed, stream_offset_bytes)``. ``model`` (npz path,
    ``prng_markov`` only) existence/validity is checked at control
    construction (server startup), not at config parse — the config
    must stay checkable on machines without the model file.
    """

    id: str
    type: str  # prng_uniform | prng_markov
    seed: str  # 128-bit hex, "0x" + 32 hex digits; REQUIRED
    model: str = ""  # npz model path; prng_markov only

    @property
    def seed_int(self) -> int:
        return int(self.seed, 16)


@dataclass
class ProfileConfig:
    """One profile: a named deterministic transform over input sources.

    ``taps`` / ``stride`` / ``allow_period4`` are parity-only params
    (validated at load; empty/zero on other transforms). ``params`` is
    REQUIRED for parity — there are no implicit tap defaults.
    """

    id: str
    transform: str  # identity | xnor | parity
    inputs: list[str]
    taps: tuple[int, ...] = ()
    stride: int = 0
    allow_period4: bool = False


@dataclass
class ProfilesDefaultsConfig:
    """Shared knobs for profile serving (paired reads, S10)."""

    chunk_bytes: int = 4096  # alternating paired-read chunk size
    max_skew_ns: int = 50_000_000  # pair-skew WARNING threshold (50 ms)


@dataclass
class Config:
    """Root configuration object."""

    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    post_processing_mode: str = "raw"
    devices: list[DeviceConfig] = field(default_factory=list)
    controls: list[ControlConfig] = field(default_factory=list)
    profiles: list[ProfileConfig] = field(default_factory=list)
    profiles_defaults: ProfilesDefaultsConfig = field(default_factory=ProfilesDefaultsConfig)
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
            {
                "server",
                "auth",
                "limits",
                "freshness",
                "post_processing",
                "devices",
                "controls",
                "profiles",
                "profiles_defaults",
                "database",
            },
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
                    "pci_address",
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
            if dev.get("pci_address") is not None and dev["type"] != "chardev":
                raise ConfigError(
                    f"devices[{dev['id']}]: `pci_address` is only valid for type "
                    f"'chardev', got type {dev['type']!r}"
                )
            if dev["type"] == "chardev" and override is not None:
                raise ConfigError(
                    f"devices[{dev['id']}]: `post_processing` does not apply to "
                    "'chardev' devices — they serve whatever the driver DMA delivers"
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
                    pci_address=(
                        str(dev["pci_address"]) if dev.get("pci_address") is not None else None
                    ),
                )
            )

        # Devices, controls and profiles share ONE id namespace: API keys
        # bind to any of them interchangeably, so collisions are refused.
        controls = _parse_controls(data.get("controls", []) or [], seen_ids)
        profiles = _parse_profiles(data.get("profiles", []) or [], seen_ids)
        profiles_defaults = _parse_profiles_defaults(data.get("profiles_defaults", {}) or {})

        db = data.get("database", {}) or {}
        _check_keys("database", db, {"path"})

        return cls(
            server=server,
            auth=auth,
            limits=limits,
            freshness=freshness,
            post_processing_mode=pp_mode,
            devices=devices,
            controls=controls,
            profiles=profiles,
            profiles_defaults=profiles_defaults,
            database_path=str(db.get("path", "./qbert0g.db")),
        )


def _parse_controls(entries: list, seen_ids: set[str]) -> list[ControlConfig]:
    """Validate the ``controls:`` section (PRNG control sources)."""
    controls: list[ControlConfig] = []
    for ctl in entries:
        section = f"controls[{ctl.get('id', '?')}]"
        _check_keys(section, ctl, {"id", "type", "seed", "model"})
        if "id" not in ctl or "type" not in ctl:
            raise ConfigError("controls: every control needs `id` and `type`")
        if ctl["type"] not in CONTROL_TYPES:
            raise ConfigError(
                f"{section}: type must be one of {sorted(CONTROL_TYPES)}, got {ctl['type']!r}"
            )
        if ctl["id"] in seen_ids:
            raise ConfigError(
                f"{section}: duplicate id {ctl['id']!r} — devices, controls and "
                "profiles share one id namespace"
            )
        seen_ids.add(ctl["id"])
        seed = ctl.get("seed")
        if not seed or not _SEED_RE.match(str(seed)):
            raise ConfigError(
                f"{section}: `seed` is required and must be a 128-bit hex literal "
                f"(`0x` + 32 hex digits) — no silent time-seeding; got {seed!r}"
            )
        if ctl["type"] == "prng_markov":
            if not ctl.get("model"):
                raise ConfigError(f"{section}: `model` (npz path) is required for prng_markov")
        elif "model" in ctl:
            raise ConfigError(f"{section}: `model` only applies to prng_markov")
        controls.append(
            ControlConfig(
                id=str(ctl["id"]),
                type=str(ctl["type"]),
                seed=str(seed),
                model=str(ctl.get("model", "")),
            )
        )
    return controls


def _parse_profiles(entries: list, seen_ids: set[str]) -> list[ProfileConfig]:
    """Validate the ``profiles:`` section (deterministic transforms).

    ``seen_ids`` already holds every device and control id: profile
    inputs must resolve there. Profiles may not reference other
    profiles (no nesting, v1) — profile ids are added to the namespace
    only AFTER input resolution, so a forward or backward profile
    reference fails the same "not a device or control" check.
    """
    input_ids = set(seen_ids)  # devices + controls only, frozen before profiles
    profiles: list[ProfileConfig] = []
    for prof in entries:
        section = f"profiles[{prof.get('id', '?')}]"
        _check_keys(section, prof, {"id", "transform", "inputs", "params"})
        if "id" not in prof or "transform" not in prof:
            raise ConfigError("profiles: every profile needs `id` and `transform`")
        transform = prof["transform"]
        if transform not in TRANSFORM_ARITY:
            raise ConfigError(
                f"{section}: transform must be one of {sorted(TRANSFORM_ARITY)}, "
                f"got {transform!r}"
            )
        if prof["id"] in seen_ids:
            raise ConfigError(
                f"{section}: duplicate id {prof['id']!r} — devices, controls and "
                "profiles share one id namespace"
            )
        seen_ids.add(prof["id"])
        inputs = [str(i) for i in prof.get("inputs", []) or []]
        arity = TRANSFORM_ARITY[transform]
        if len(inputs) != arity:
            raise ConfigError(
                f"{section}: transform {transform!r} takes exactly {arity} "
                f"input(s), got {len(inputs)}"
            )
        for input_id in inputs:
            if input_id not in input_ids:
                raise ConfigError(
                    f"{section}: input {input_id!r} is not a configured device or "
                    "control (profiles cannot reference profiles — no nesting)"
                )
        params = prof.get("params", {}) or {}
        if transform == "parity":
            taps, stride, allow_period4 = _parse_parity_params(section, params)
        else:
            if params:
                raise ConfigError(f"{section}: `params` only applies to the parity transform")
            taps, stride, allow_period4 = (), 0, False
        profiles.append(
            ProfileConfig(
                id=str(prof["id"]),
                transform=str(transform),
                inputs=inputs,
                taps=taps,
                stride=stride,
                allow_period4=allow_period4,
            )
        )
    return profiles


def _parse_parity_params(section: str, params: dict) -> tuple[tuple[int, ...], int, bool]:
    """Validate parity ``params`` including the period-4 guard.

    The guard rejects any pairwise tap distance or stride that is a
    multiple of 4, protecting against a known hardware periodicity.
    ``allow_period4: true`` is the deliberate escape hatch — e.g. the
    canonical phase-covering design (taps spanning all residues mod 4
    with stride 4) trips the literal stride rule on purpose.
    """
    _check_keys(f"{section}.params", params, {"taps", "stride", "allow_period4"})
    if "taps" not in params or "stride" not in params:
        raise ConfigError(f"{section}: parity requires explicit `params.taps` and `params.stride`")
    raw_taps = params["taps"]
    if (
        not isinstance(raw_taps, list)
        or not raw_taps
        or not all(isinstance(t, int) and not isinstance(t, bool) and t >= 0 for t in raw_taps)
    ):
        raise ConfigError(f"{section}: `taps` must be a non-empty list of non-negative integers")
    taps = tuple(raw_taps)
    if any(b <= a for a, b in zip(taps, taps[1:], strict=False)):
        raise ConfigError(f"{section}: `taps` must be strictly increasing, got {list(taps)}")
    stride = params["stride"]
    if not isinstance(stride, int) or isinstance(stride, bool) or stride < 1:
        raise ConfigError(f"{section}: `stride` must be an integer >= 1, got {stride!r}")
    allow_period4 = bool(params.get("allow_period4", False))
    if not allow_period4:
        if stride % 4 == 0:
            raise ConfigError(
                f"{section}: stride {stride} is a multiple of 4 (known hardware "
                "periodicity); set `params.allow_period4: true` if this is deliberate"
            )
        for i, a in enumerate(taps):
            for b in taps[i + 1 :]:
                if (b - a) % 4 == 0:
                    raise ConfigError(
                        f"{section}: tap distance {b - a} (taps {a} and {b}) is a "
                        "multiple of 4 (known hardware periodicity); set "
                        "`params.allow_period4: true` if this is deliberate"
                    )
    return taps, stride, allow_period4


def _parse_profiles_defaults(pd: dict) -> ProfilesDefaultsConfig:
    """Validate the ``profiles_defaults:`` section."""
    _check_keys("profiles_defaults", pd, {"chunk_bytes", "max_skew_ns"})
    defaults = ProfilesDefaultsConfig(
        chunk_bytes=int(pd.get("chunk_bytes", 4096)),
        max_skew_ns=int(pd.get("max_skew_ns", 50_000_000)),
    )
    if defaults.chunk_bytes < 1:
        raise ConfigError("profiles_defaults: `chunk_bytes` must be >= 1")
    if defaults.max_skew_ns < 0:
        raise ConfigError("profiles_defaults: `max_skew_ns` must be >= 0")
    return defaults
