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
#: Deferred (spec §12): >2-ary transforms and profile nesting are
#: deliberately out of scope for v1 — new arities would be added here.
TRANSFORM_ARITY = {"identity": 1, "xnor": 2, "parity": 1}

#: All integrator statistics implemented in integrators.py. The name→
#: implementation dispatch lives THERE; the name sets live HERE so config
#: validation and the statistics library share one source of truth
#: (the CONTROL_TYPES / TRANSFORM_ARITY pattern).
INTEGRATOR_TYPES = frozenset(
    {"bit_z", "byte_z", "cusum", "rw_excursion", "majority_vote", "kmer_mode"}
)

#: Integrators allowed to produce the served ``u`` on the PurityService
#: draw path. Everything else is refused at startup / INVALID_ARGUMENT —
#: nobody samples tokens off a mode statistic.
SERVE_INTEGRATORS = frozenset({"bit_z", "byte_z"})

#: Auxiliary statistics computable alongside the primary
#: (``integration.secondaries``). Provenance/CLI visibility only —
#: their z/u fields are defined but never served.
AUX_INTEGRATORS = frozenset({"cusum", "rw_excursion"})
# majority_vote / kmer_mode: dispatch entries exist for offline CLI use
# only; the serve path refuses them (they are in neither set above).

#: Quantum-fraction tiers a fingerprint may declare. Devices ship
#: ``unrated`` until a characterization supplies a value.
QF_TIERS = frozenset({"99+", "98+", "95+", "90+", "unrated"})

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
    fingerprint: str = ""  # per-device fingerprint JSON path (QPI draws)


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
    fingerprint: str = ""  # fingerprint JSON path (QPI draws on a control)

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
class ProvenanceConfig:
    """The append-only per-request provenance JSONL (see sources.py).

    ``strict: false`` (default): a provenance write failure logs ERROR
    and the request is still served. ``strict: true`` inverts that for
    study runs — a request whose provenance cannot be recorded fails.
    """

    path: str = "./provenance.jsonl"
    strict: bool = False


@dataclass
class IntegrationConfig:
    """Server-side integration (QPI draws over the PurityService).

    ``sources`` is the allowlist of ids drawable via ``GetDraw`` — each
    must be a configured device or control whose entry declares a
    ``fingerprint:`` path (checked structurally at config parse; the
    file itself must load and validate at server startup — see
    fingerprint.py — mirroring the prng_markov ``model`` precedent so
    the config stays checkable on machines without the files).
    ``default_integrator`` must be a serve-path integrator: aux/offline
    statistics never produce a served ``u``.
    """

    block_bytes: int = 2_097_152  # 2 MiB per draw (z ≈ 8192·δ sensitivity)
    default_integrator: str = "bit_z"
    secondaries: list[str] = field(default_factory=list)  # subset of AUX_INTEGRATORS
    sources: list[str] = field(default_factory=list)  # ids drawable via PurityService


@dataclass
class CoherenceConfig:
    """Background device-pair block-correlation monitor (coherence.py)."""

    enabled: bool = False
    pair: list[str] = field(default_factory=list)  # exactly 2 device ids when enabled
    block_bytes: int = 1024  # ones-fraction reduction block size
    blocks_per_side: int = 32  # blocks read per device per evaluation
    lag_scan_blocks: int = 4  # Pearson r scanned at lags in [-N, +N]
    min_valid_blocks: int = 24  # k_eff below this ⇒ evaluation invalid
    refresh_s: float = 1.0  # evaluation cadence
    max_age_s: float = 5.0  # staleness bound: older values serve coherence_valid=false
    null_ref: str | None = None  # `coherence null` output path (informational)
    null_pair: list[str] = field(default_factory=list)  # 2 prng control ids for the null


@dataclass
class PurityConfig:
    """Purity-taxonomy serve-time knobs (purity.py)."""

    verify_sigma: float = 6.0  # |z| tolerance for the quantum_verified bit


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
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)
    database_path: str = "./qbert0g.db"
    integration: IntegrationConfig = field(default_factory=IntegrationConfig)
    coherence: CoherenceConfig = field(default_factory=CoherenceConfig)
    purity: PurityConfig = field(default_factory=PurityConfig)

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
                "provenance",
                "database",
                "integration",
                "coherence",
                "purity",
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
                    "fingerprint",
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
                    fingerprint=str(dev.get("fingerprint", "") or ""),
                )
            )

        # Devices, controls and profiles share ONE id namespace: API keys
        # bind to any of them interchangeably, so collisions are refused.
        controls = _parse_controls(data.get("controls", []) or [], seen_ids)
        profiles = _parse_profiles(data.get("profiles", []) or [], seen_ids)
        profiles_defaults = _parse_profiles_defaults(data.get("profiles_defaults", {}) or {})

        prov = data.get("provenance", {}) or {}
        _check_keys("provenance", prov, {"path", "strict"})
        provenance = ProvenanceConfig(
            path=str(prov.get("path", "./provenance.jsonl")),
            strict=bool(prov.get("strict", False)),
        )
        if not provenance.path:
            raise ConfigError("provenance: `path` must not be empty")

        db = data.get("database", {}) or {}
        _check_keys("database", db, {"path"})

        integration = _parse_integration(
            data.get("integration", {}) or {}, devices=devices, controls=controls
        )
        coherence = _parse_coherence(
            data.get("coherence", {}) or {}, devices=devices, controls=controls
        )
        purity = _parse_purity(data.get("purity", {}) or {})

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
            provenance=provenance,
            database_path=str(db.get("path", "./qbert0g.db")),
            integration=integration,
            coherence=coherence,
            purity=purity,
        )


def _parse_controls(entries: list, seen_ids: set[str]) -> list[ControlConfig]:
    """Validate the ``controls:`` section (PRNG control sources)."""
    controls: list[ControlConfig] = []
    for ctl in entries:
        section = f"controls[{ctl.get('id', '?')}]"
        _check_keys(section, ctl, {"id", "type", "seed", "model", "fingerprint"})
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
                fingerprint=str(ctl.get("fingerprint", "") or ""),
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


def _parse_integration(
    data: dict, *, devices: list[DeviceConfig], controls: list[ControlConfig]
) -> IntegrationConfig:
    """Validate the ``integration:`` section (QPI draw serving).

    Startup rule (FR-Q3): every id in ``sources`` must name a configured
    device or control whose entry declares ``fingerprint:`` — no silent
    ideal-value defaults. The fingerprint FILE is loaded and validated
    at server startup (fingerprint.load_config_fingerprints), not here,
    so the config stays checkable on machines without the files.
    Profiles are not drawable in this iteration.
    """
    _check_keys(
        "integration", data, {"block_bytes", "default_integrator", "secondaries", "sources"}
    )
    integration = IntegrationConfig(
        block_bytes=int(data.get("block_bytes", 2_097_152)),
        default_integrator=str(data.get("default_integrator", "bit_z")),
        secondaries=[str(s) for s in data.get("secondaries", []) or []],
        sources=[str(s) for s in data.get("sources", []) or []],
    )
    if integration.block_bytes < 1:
        raise ConfigError("integration: `block_bytes` must be >= 1")
    if integration.default_integrator not in INTEGRATOR_TYPES:
        raise ConfigError(
            f"integration: default_integrator must be one of {sorted(INTEGRATOR_TYPES)}, "
            f"got {integration.default_integrator!r}"
        )
    if integration.default_integrator not in SERVE_INTEGRATORS:
        raise ConfigError(
            f"integration: default_integrator {integration.default_integrator!r} is not a "
            f"serve-path integrator (allowed: {sorted(SERVE_INTEGRATORS)}) — aux/offline "
            "statistics never produce a served u"
        )
    for name in integration.secondaries:
        if name not in AUX_INTEGRATORS:
            raise ConfigError(
                f"integration: secondaries entry {name!r} must be one of "
                f"{sorted(AUX_INTEGRATORS)} (aux statistics only)"
            )
    fingerprint_by_id = {d.id: d.fingerprint for d in devices}
    fingerprint_by_id.update({c.id: c.fingerprint for c in controls})
    seen_sources: set[str] = set()
    for source_id in integration.sources:
        if source_id in seen_sources:
            raise ConfigError(f"integration: duplicate sources entry {source_id!r}")
        seen_sources.add(source_id)
        if source_id not in fingerprint_by_id:
            raise ConfigError(
                f"integration: sources entry {source_id!r} is not a configured device "
                "or control (profiles are not drawable in this iteration)"
            )
        if not fingerprint_by_id[source_id]:
            raise ConfigError(
                f"integration: sources entry {source_id!r} has no `fingerprint:` path — "
                "a drawable source REQUIRES a fitted fingerprint "
                "(scripts/fit_fingerprint.py); there are no silent ideal-value defaults"
            )
    return integration


def _parse_coherence(
    data: dict, *, devices: list[DeviceConfig], controls: list[ControlConfig]
) -> CoherenceConfig:
    """Validate the ``coherence:`` section (device-pair monitor)."""
    _check_keys(
        "coherence",
        data,
        {
            "enabled",
            "pair",
            "block_bytes",
            "blocks_per_side",
            "lag_scan_blocks",
            "min_valid_blocks",
            "refresh_s",
            "max_age_s",
            "null_ref",
            "null_pair",
        },
    )
    null_ref = data.get("null_ref")
    coherence = CoherenceConfig(
        enabled=bool(data.get("enabled", False)),
        pair=[str(p) for p in data.get("pair", []) or []],
        block_bytes=int(data.get("block_bytes", 1024)),
        blocks_per_side=int(data.get("blocks_per_side", 32)),
        lag_scan_blocks=int(data.get("lag_scan_blocks", 4)),
        min_valid_blocks=int(data.get("min_valid_blocks", 24)),
        refresh_s=float(data.get("refresh_s", 1.0)),
        max_age_s=float(data.get("max_age_s", 5.0)),
        null_ref=str(null_ref) if null_ref is not None else None,
        null_pair=[str(p) for p in data.get("null_pair", []) or []],
    )
    if coherence.block_bytes < 1:
        raise ConfigError("coherence: `block_bytes` must be >= 1")
    if coherence.blocks_per_side < 1:
        raise ConfigError("coherence: `blocks_per_side` must be >= 1")
    if coherence.lag_scan_blocks < 0:
        raise ConfigError("coherence: `lag_scan_blocks` must be >= 0")
    if coherence.min_valid_blocks < 1:
        raise ConfigError("coherence: `min_valid_blocks` must be >= 1")
    if coherence.min_valid_blocks > coherence.blocks_per_side:
        raise ConfigError(
            "coherence: `min_valid_blocks` must be <= `blocks_per_side` "
            "(no evaluation could ever be valid)"
        )
    if coherence.refresh_s <= 0:
        raise ConfigError("coherence: `refresh_s` must be > 0")
    if coherence.max_age_s <= 0:
        raise ConfigError("coherence: `max_age_s` must be > 0")
    device_ids = {d.id for d in devices}
    if coherence.enabled:
        if len(coherence.pair) != 2 or len(set(coherence.pair)) != 2:
            raise ConfigError(
                "coherence: `pair` must list exactly 2 distinct device ids when enabled"
            )
        for pair_id in coherence.pair:
            if pair_id not in device_ids:
                raise ConfigError(
                    f"coherence: pair entry {pair_id!r} is not a configured device"
                )
    if coherence.null_pair:
        if len(coherence.null_pair) != 2 or len(set(coherence.null_pair)) != 2:
            raise ConfigError(
                "coherence: `null_pair` must be empty or exactly 2 distinct control ids"
            )
        control_ids = {c.id for c in controls}
        for null_id in coherence.null_pair:
            if null_id not in control_ids:
                raise ConfigError(
                    f"coherence: null_pair entry {null_id!r} is not a configured control "
                    "(the null distribution runs over PRNG controls only)"
                )
    return coherence


def _parse_purity(data: dict) -> PurityConfig:
    """Validate the ``purity:`` section."""
    _check_keys("purity", data, {"verify_sigma"})
    purity = PurityConfig(verify_sigma=float(data.get("verify_sigma", 6.0)))
    if purity.verify_sigma <= 0:
        raise ConfigError("purity: `verify_sigma` must be > 0")
    return purity


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
