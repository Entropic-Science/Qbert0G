"""Per-source statistical fingerprints for the QPI integration layer.

A fingerprint is a small JSON file, fitted OFFLINE from raw device
dumps by ``scripts/fit_fingerprint.py``, that freezes a source's
baseline statistics: ones-fraction, byte mean/std, bit autocorrelation
at a fixed lag set, and the precomputed effective-sample-size factor

    ``neff_factor = 1 / (1 + 2 * sum(rho_k))``

used by the integrators to correct standard errors for serial
correlation. Baselines are **frozen at load** â€” there is no runtime
adaptation (a drifting baseline absorbs exactly the sustained signal
the integrators exist to see). Re-characterization is an operator
action between runs.

Validation is loud (:class:`~qbert0g.config.ConfigError`), including a
cross-check that the stored ``neff_factor`` matches the stored ACF
within ``1e-6`` â€” a silently hand-edited file is refused. The file's
SHA-256 is returned alongside the fingerprint so every draw's
provenance record can pin exactly which baseline was in force.

Layering: this module imports ``config`` only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import QF_TIERS, Config, ConfigError

#: Stored-vs-recomputed neff_factor tolerance (refuse edited files).
NEFF_TOLERANCE = 1e-6

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_REQUIRED_KEYS = {
    "device_id",
    "firmware",
    "ones_fraction",
    "byte_mean",
    "byte_std",
    "bit_acf",
    "neff_factor",
    "quantum_fraction_tier",
    "source_dumps_sha256",
    "fitted_utc",
    "session_ref",
}


@dataclass(frozen=True)
class Fingerprint:
    """A frozen statistical baseline for one entropy source."""

    device_id: str
    firmware: str
    ones_fraction: float  # P(bit = 1), in (0, 1)
    byte_mean: float  # uint8 mean, in [0, 255]
    byte_std: float  # uint8 population std, > 0
    bit_acf: dict[int, float]  # lag -> rho
    neff_factor: float  # 1 / (1 + 2*sum(rho_k)), precomputed by the fit script
    quantum_fraction_tier: str  # in QF_TIERS
    source_dumps_sha256: tuple[str, ...]
    fitted_utc: str
    session_ref: str


def neff_factor_from_acf(bit_acf: dict[int, float]) -> float:
    """``1 / (1 + 2*sum(rho_k))`` â€” the fit script's exact arithmetic.

    Raises :class:`ConfigError` when the ACF sum leaves no positive
    effective sample size (``1 + 2*sum <= 0``): such a fingerprint
    cannot correct any standard error and must be refused.
    """
    denominator = 1.0 + 2.0 * sum(bit_acf.values())
    if denominator <= 0.0:
        raise ConfigError(
            f"fingerprint: bit_acf sum {sum(bit_acf.values()):.6f} leaves no positive "
            "effective sample size (1 + 2*sum(rho) <= 0)"
        )
    return 1.0 / denominator


def load_fingerprint(path: str) -> tuple[Fingerprint, str]:
    """Load and validate one fingerprint JSON.

    Returns ``(fingerprint, file_sha256)``; the hash is attached to
    every draw's provenance record. Any missing/unknown/invalid field
    â€” including a ``neff_factor`` inconsistent with the stored ACF â€”
    raises :class:`ConfigError`.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"fingerprint: file not found: {path}")
    raw = file_path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"fingerprint {path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"fingerprint {path}: top level must be a JSON object")
    missing = _REQUIRED_KEYS - set(data)
    if missing:
        raise ConfigError(f"fingerprint {path}: missing key(s) {sorted(missing)}")
    unknown = set(data) - _REQUIRED_KEYS
    if unknown:
        raise ConfigError(f"fingerprint {path}: unknown key(s) {sorted(unknown)}")

    ones_fraction = _as_float(path, "ones_fraction", data["ones_fraction"])
    if not 0.0 < ones_fraction < 1.0:
        raise ConfigError(
            f"fingerprint {path}: ones_fraction must be in (0, 1), got {ones_fraction}"
        )
    byte_mean = _as_float(path, "byte_mean", data["byte_mean"])
    if not 0.0 <= byte_mean <= 255.0:
        raise ConfigError(f"fingerprint {path}: byte_mean must be in [0, 255], got {byte_mean}")
    byte_std = _as_float(path, "byte_std", data["byte_std"])
    if byte_std <= 0.0:
        raise ConfigError(f"fingerprint {path}: byte_std must be > 0, got {byte_std}")

    bit_acf = _parse_bit_acf(path, data["bit_acf"])
    neff_factor = _as_float(path, "neff_factor", data["neff_factor"])
    if neff_factor <= 0.0:
        raise ConfigError(f"fingerprint {path}: neff_factor must be > 0, got {neff_factor}")
    expected_neff = neff_factor_from_acf(bit_acf)
    if abs(neff_factor - expected_neff) > NEFF_TOLERANCE:
        raise ConfigError(
            f"fingerprint {path}: stored neff_factor {neff_factor!r} does not match the "
            f"stored bit_acf (expected {expected_neff:.9f} within {NEFF_TOLERANCE}) â€” "
            "refusing a silently edited file; re-fit with scripts/fit_fingerprint.py"
        )

    tier = str(data["quantum_fraction_tier"])
    if tier not in QF_TIERS:
        raise ConfigError(
            f"fingerprint {path}: quantum_fraction_tier must be one of {sorted(QF_TIERS)}, "
            f"got {tier!r}"
        )

    dumps = data["source_dumps_sha256"]
    if not isinstance(dumps, list) or not all(
        isinstance(h, str) and _SHA256_RE.match(h) for h in dumps
    ):
        raise ConfigError(
            f"fingerprint {path}: source_dumps_sha256 must be a list of sha256 hex digests"
        )

    fingerprint = Fingerprint(
        device_id=str(data["device_id"]),
        firmware=str(data["firmware"]),
        ones_fraction=ones_fraction,
        byte_mean=byte_mean,
        byte_std=byte_std,
        bit_acf=bit_acf,
        neff_factor=neff_factor,
        quantum_fraction_tier=tier,
        source_dumps_sha256=tuple(dumps),
        fitted_utc=str(data["fitted_utc"]),
        session_ref=str(data["session_ref"]),
    )
    return fingerprint, sha256


def load_config_fingerprints(config: Config) -> dict[str, tuple[Fingerprint, str]]:
    """Load the fingerprint of every ``integration.sources`` id.

    The startup half of FR-Q3: config parsing already guaranteed each
    source id declares a ``fingerprint:`` path; here the files must
    exist, load, and validate â€” otherwise :class:`ConfigError` and the
    server does not start. Returns ``{source_id: (fingerprint, sha256)}``.
    """
    path_by_id = {d.id: d.fingerprint for d in config.devices}
    path_by_id.update({c.id: c.fingerprint for c in config.controls})
    loaded: dict[str, tuple[Fingerprint, str]] = {}
    for source_id in config.integration.sources:
        path = path_by_id.get(source_id, "")
        if not path:  # unreachable after config validation; fail loud anyway
            raise ConfigError(
                f"integration: sources entry {source_id!r} has no `fingerprint:` path"
            )
        try:
            loaded[source_id] = load_fingerprint(path)
        except ConfigError as exc:
            raise ConfigError(f"integration: sources entry {source_id!r}: {exc}") from exc
    return loaded


def _as_float(path: str, key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"fingerprint {path}: {key} must be a number, got {value!r}")
    return float(value)


def _parse_bit_acf(path: str, raw: object) -> dict[int, float]:
    """JSON object keys are strings; lags must parse to positive ints."""
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"fingerprint {path}: bit_acf must be a non-empty lag->rho object")
    bit_acf: dict[int, float] = {}
    for key, value in raw.items():
        try:
            lag = int(key)
        except (TypeError, ValueError):
            raise ConfigError(
                f"fingerprint {path}: bit_acf lag {key!r} is not an integer"
            ) from None
        if lag < 1:
            raise ConfigError(f"fingerprint {path}: bit_acf lag {lag} must be >= 1")
        rho = _as_float(path, f"bit_acf[{lag}]", value)
        if not -1.0 < rho < 1.0:
            raise ConfigError(
                f"fingerprint {path}: bit_acf[{lag}] must be in (-1, 1), got {rho}"
            )
        bit_acf[lag] = rho
    return bit_acf
