"""Entropy purity taxonomy: frozen label vocabulary + derivation.

Labels RECORD what a source is; they never gate what it may do â€” any
source is integrable, and the operator reads labels, the server does
not moralize. Canonical serialization:

    ``origin/integrity/processing[/expanded][/amplified:<n>]/qf:<tier>[/QV]``

Static axes are derived per source at STARTUP from config (+ optional
fingerprint); the per-request bits â€” ``amplified``/``integration_n``
and ``quantum_verified`` â€” are resolved at serve time by the
PurityService.

Static derivation mapping (documented here, tested in
tests/test_purity.py):

- **devices** â€” origin ``quantum`` (``mock`` -> ``true_random``);
  ``sha256`` post-processing anywhere in the path -> integrity
  ``scrambled`` + processing ``uniform`` (cryptographic whitening
  launders the raw statistics into uniformity); ``raw`` /
  ``raw_samples`` / chardev DMA -> ``intact`` + ``raw``.
- **controls** â€” ``pseudo``/``intact``/``raw`` (a seeded PRNG stream
  is served exactly as generated).
- **profiles** â€” combined from input labels, worst-case on every axis:
  any ``pseudo`` input makes the profile ``pseudo`` (then any
  ``true_random`` beats ``quantum``); any ``scrambled`` input makes it
  ``scrambled`` â€” the transforms themselves (identity/xnor/parity) are
  invertible-arithmetic-class and KEEP ``intact`` (recorded, not
  moralized); processing is the most-processed input; the qf tier is
  the worst input tier.
- ``expanded`` (a DRBG stretched a hardware seed) is ``False`` for
  every v1 source â€” no DRBG exists in this server.

Layering: imports ``config`` and ``fingerprint`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from .config import QF_TIERS, ConfigError
from .fingerprint import Fingerprint


class Origin(StrEnum):
    QUANTUM = "quantum"
    TRUE_RANDOM = "true_random"
    PSEUDO = "pseudo"


class Integrity(StrEnum):
    INTACT = "intact"
    SCRAMBLED = "scrambled"


class Processing(StrEnum):
    RAW = "raw"
    CONDITIONED = "conditioned"
    DEBIASED = "debiased"
    UNIFORM = "uniform"


#: Worst-case orderings for profile label combination (best -> worst /
#: least -> most processed).
_ORIGIN_WORST = [Origin.QUANTUM, Origin.TRUE_RANDOM, Origin.PSEUDO]
_PROCESSING_WORST = [
    Processing.RAW,
    Processing.CONDITIONED,
    Processing.DEBIASED,
    Processing.UNIFORM,
]
_TIER_WORST = ["99+", "98+", "95+", "90+", "unrated"]


@dataclass(frozen=True)
class EntropyLabel:
    """One source's purity label; static axes + per-request bits."""

    origin: Origin
    integrity: Integrity
    processing: Processing
    expanded: bool
    quantum_fraction_tier: str
    amplified: bool = False
    integration_n: int = 0  # bytes integrated per served value (when amplified)
    quantum_verified: bool = False

    def __post_init__(self) -> None:
        if self.quantum_fraction_tier not in QF_TIERS:
            raise ConfigError(
                f"label: quantum_fraction_tier must be one of {sorted(QF_TIERS)}, "
                f"got {self.quantum_fraction_tier!r}"
            )

    def canonical(self) -> str:
        """``origin/integrity/processing[/expanded][/amplified:<n>]/qf:<tier>[/QV]``."""
        parts = [self.origin.value, self.integrity.value, self.processing.value]
        if self.expanded:
            parts.append("expanded")
        if self.amplified:
            parts.append(f"amplified:{self.integration_n}")
        parts.append(f"qf:{self.quantum_fraction_tier}")
        if self.quantum_verified:
            parts.append("QV")
        return "/".join(parts)

    @classmethod
    def from_canonical(cls, label: str) -> EntropyLabel:
        """Parse a canonical string back into a label (exact round-trip)."""
        parts = label.split("/")
        if len(parts) < 4:
            raise ValueError(f"not a canonical entropy label: {label!r}")
        try:
            origin, integrity, processing = (
                Origin(parts[0]),
                Integrity(parts[1]),
                Processing(parts[2]),
            )
        except ValueError:
            raise ValueError(f"not a canonical entropy label: {label!r}") from None
        expanded = False
        amplified = False
        integration_n = 0
        tier: str | None = None
        quantum_verified = False
        for token in parts[3:]:
            if token == "expanded":
                expanded = True
            elif token.startswith("amplified:"):
                amplified = True
                integration_n = int(token.removeprefix("amplified:"))
            elif token.startswith("qf:"):
                tier = token.removeprefix("qf:")
            elif token == "QV":
                quantum_verified = True
            else:
                raise ValueError(f"unknown label token {token!r} in {label!r}")
        if tier is None:
            raise ValueError(f"canonical label missing qf tier: {label!r}")
        return cls(
            origin=origin,
            integrity=integrity,
            processing=processing,
            expanded=expanded,
            quantum_fraction_tier=tier,
            amplified=amplified,
            integration_n=integration_n,
            quantum_verified=quantum_verified,
        )


def derive_static_label(
    source_kind: str,
    device_type: str | None = None,
    post_processing_mode: str | None = None,
    fingerprint: Fingerprint | None = None,
    input_labels: tuple[EntropyLabel, ...] = (),
) -> EntropyLabel:
    """Derive one source's static label per the module-docstring mapping.

    *source_kind* is ``"device"`` / ``"control"`` / ``"profile"``.
    Devices take *device_type* and the effective *post_processing_mode*
    (the resolved override-or-global mode; ``None`` for chardev, which
    has no qcc-cli chain). Profiles take the *input_labels* of their
    (already-derived) inputs. A fingerprint, when present, supplies the
    quantum-fraction tier; absent -> ``unrated``.
    """
    tier = fingerprint.quantum_fraction_tier if fingerprint is not None else "unrated"
    if source_kind == "device":
        if device_type is None:
            raise ValueError("derive_static_label: a device label needs device_type")
        origin = Origin.TRUE_RANDOM if device_type == "mock" else Origin.QUANTUM
        if post_processing_mode == "sha256":
            integrity, processing = Integrity.SCRAMBLED, Processing.UNIFORM
        else:
            integrity, processing = Integrity.INTACT, Processing.RAW
        return EntropyLabel(
            origin=origin,
            integrity=integrity,
            processing=processing,
            expanded=False,
            quantum_fraction_tier=tier,
        )
    if source_kind == "control":
        return EntropyLabel(
            origin=Origin.PSEUDO,
            integrity=Integrity.INTACT,
            processing=Processing.RAW,
            expanded=False,
            quantum_fraction_tier=tier,
        )
    if source_kind == "profile":
        if not input_labels:
            raise ValueError("derive_static_label: a profile label needs input_labels")
        origin = max((lbl.origin for lbl in input_labels), key=_ORIGIN_WORST.index)
        integrity = (
            Integrity.SCRAMBLED
            if any(lbl.integrity is Integrity.SCRAMBLED for lbl in input_labels)
            else Integrity.INTACT
        )
        processing = max(
            (lbl.processing for lbl in input_labels), key=_PROCESSING_WORST.index
        )
        profile_tier = max(
            (lbl.quantum_fraction_tier for lbl in input_labels), key=_TIER_WORST.index
        )
        return EntropyLabel(
            origin=origin,
            integrity=integrity,
            processing=processing,
            expanded=any(lbl.expanded for lbl in input_labels),
            quantum_fraction_tier=profile_tier,
        )
    raise ValueError(
        f"derive_static_label: source_kind must be device/control/profile, got {source_kind!r}"
    )


def resolve_request_bits(
    static: EntropyLabel, *, integration_n: int, quantum_verified: bool
) -> EntropyLabel:
    """Serve-time copy of a static label with the per-request bits set."""
    return replace(
        static,
        amplified=True,
        integration_n=integration_n,
        quantum_verified=quantum_verified,
    )
