"""Integration statistics: raw entropy blocks -> (z, u, aux).

Pure functions plus a name-dispatch factory, no I/O â€” the "registry"
is the constant sets in :mod:`qbert0g.config` (``INTEGRATOR_TYPES`` /
``SERVE_INTEGRATORS`` / ``AUX_INTEGRATORS``), following the repo's
``CONTROL_TYPES`` + explicit-factory pattern.

Every statistic is referenced to a frozen :class:`~qbert0g.fingerprint.
Fingerprint` baseline â€” never to ideal values â€” and serial correlation
is corrected through the fingerprint's precomputed ``neff_factor``
(effective sample size ``n_eff = n * neff_factor``).

Serve-path integrators (``bit_z``, ``byte_z``) map z to a uniform via
the exact erf-based normal CDF, clamped to ``(1e-10, 1 - 1e-10)`` â€” a
served u is therefore never 0.0 or 1.0. Aux integrators (``cusum``,
``rw_excursion``) carry their statistic in ``aux`` for provenance and
monitoring; their z/u fields are defined but never served. The offline
statistics (``majority_vote``, ``kmer_mode``) exist for CLI analysis
only â€” the serve path refuses them by set membership.

Layering: imports ``config`` and ``fingerprint`` only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import INTEGRATOR_TYPES
from .fingerprint import Fingerprint

_SQRT2 = math.sqrt(2.0)

#: Served u values are clamped to (U_CLAMP, 1 - U_CLAMP): a degenerate
#: CDF extreme must never reach a consumer (and absence of u on the
#: wire stays unambiguous â€” proto3 omits 0.0).
U_CLAMP = 1e-10


@dataclass(frozen=True)
class IntegrationResult:
    """One integrated block: z-score, uniform value, aux statistics."""

    z: float
    u: float
    aux: dict[str, float]


def integrate(name: str, raw: bytes, fp: Fingerprint) -> IntegrationResult:
    """Dispatch *raw* through the integrator *name* against baseline *fp*."""
    if name not in INTEGRATOR_TYPES:
        raise ValueError(
            f"unknown integrator {name!r} (known: {sorted(INTEGRATOR_TYPES)})"
        )
    if not raw:
        raise ValueError("cannot integrate an empty block")
    return _INTEGRATORS[name](raw, fp)


def phi(z: float) -> float:
    """Exact erf-based normal CDF, clamped to ``(U_CLAMP, 1 - U_CLAMP)``."""
    u = 0.5 * (1.0 + math.erf(z / _SQRT2))
    return min(max(u, U_CLAMP), 1.0 - U_CLAMP)


def _bit_z(raw: bytes, fp: Fingerprint) -> IntegrationResult:
    """Ones-fraction z against the fingerprint baseline (the default).

    ``ones`` via ``int.from_bytes(raw, "big").bit_count()`` â€” on
    CPython 3.11+ the big-int popcount runs a few times faster than
    ``np.unpackbits(...).sum()`` for MiB-scale blocks and allocates no
    8x-expanded bit array; either is numerically identical.
    """
    n_bits = 8 * len(raw)
    ones = int.from_bytes(raw, "big").bit_count()
    f = ones / n_bits
    f0 = fp.ones_fraction
    se = math.sqrt(f0 * (1.0 - f0) / (n_bits * fp.neff_factor))
    z = (f - f0) / se
    return IntegrationResult(z=z, u=phi(z), aux={"ones_fraction": f})


def _byte_z(raw: bytes, fp: Fingerprint) -> IntegrationResult:
    """The historical qr-sampler byte-mean statistic (continuity).

    Ported verbatim from qr-sampler's ``ZScoreMeanAmplifier`` (uint8
    mean, ``sem = population_std / sqrt(n)``, erf-based CDF), but
    referenced to the fingerprint's ``byte_mean`` / ``byte_std`` and
    corrected for serial correlation the same way as ``bit_z``:
    ``sem /= sqrt(neff_factor)``, i.e. ``sem = byte_std /
    sqrt(n * neff_factor)``.
    """
    samples = np.frombuffer(raw, dtype=np.uint8)
    n = len(samples)
    sample_mean = float(np.mean(samples))
    sem = fp.byte_std / math.sqrt(n)
    sem /= math.sqrt(fp.neff_factor)
    z = (sample_mean - fp.byte_mean) / sem
    return IntegrationResult(z=z, u=phi(z), aux={"sample_mean": sample_mean})


def _cusum(raw: bytes, fp: Fingerprint) -> IntegrationResult:
    """Change-point statistic: max |cumulative sum| of baseline-centered bits.

    Bits are centered on the fingerprint ones-fraction (``x_i = b_i -
    f0``) so a fingerprint-consistent stream is drift-free; the
    statistic is the maximum absolute partial sum normalized by the
    null standard deviation of the full-block sum,
    ``sqrt(n_bits * f0 * (1 - f0))``. Sensitive to INTERMITTENT
    influence â€” pressure applied then released nets out of the block
    mean but not out of the path. Aux-only: z/u defined, never served.
    """
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8)).astype(np.float64)
    f0 = fp.ones_fraction
    walk = np.cumsum(bits - f0)
    stat = float(np.max(np.abs(walk))) / math.sqrt(len(bits) * f0 * (1.0 - f0))
    return IntegrationResult(z=stat, u=phi(stat), aux={"cusum": stat})


def _rw_excursion(raw: bytes, fp: Fingerprint) -> IntegrationResult:
    """Max |excursion| of the raw Â±1 bit walk, normalized by sqrt(n_bits).

    Unlike :func:`_cusum` the walk is NOT baseline-centered â€” it is the
    plain random-walker's range over the block. Aux-only.
    """
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8)).astype(np.float64)
    walk = np.cumsum(2.0 * bits - 1.0)
    stat = float(np.max(np.abs(walk))) / math.sqrt(len(bits))
    return IntegrationResult(z=stat, u=phi(stat), aux={"rw_excursion": stat})


def _majority_vote(raw: bytes, fp: Fingerprint) -> IntegrationResult:
    """Block majority vote against the baseline ones-fraction. Offline only.

    A quantizer stacked on the walker â€” strictly weaker than the mean
    as a detector, useful where a binary verdict is wanted. The vote is
    1.0 when the block ones-fraction meets or exceeds ``f0``; z is the
    corresponding Â±1 and u the clamped extreme. Never on the serve path.
    """
    n_bits = 8 * len(raw)
    f = int.from_bytes(raw, "big").bit_count() / n_bits
    vote = 1.0 if f >= fp.ones_fraction else 0.0
    z = 1.0 if vote else -1.0
    u = (1.0 - U_CLAMP) if vote else U_CLAMP
    return IntegrationResult(z=z, u=u, aux={"majority": vote, "ones_fraction": f})


def _kmer_mode(raw: bytes, fp: Fingerprint) -> IntegrationResult:
    """Modal byte value and its frequency (k=8-bit k-mer mode). Offline only.

    Statistically weak for bias detection (near uniformity the mode's
    location is mostly multinomial noise) but retained as a cheap
    secondary for the value-choosing-influence hypothesis the mean is
    blind to. Diagnostic aux only; z/u are inert (0.0 / 0.5).
    """
    samples = np.frombuffer(raw, dtype=np.uint8)
    counts = np.bincount(samples, minlength=256)
    mode = int(np.argmax(counts))
    mode_fraction = float(counts[mode]) / len(samples)
    return IntegrationResult(
        z=0.0, u=0.5, aux={"mode_byte": float(mode), "mode_fraction": mode_fraction}
    )


_INTEGRATORS = {
    "bit_z": _bit_z,
    "byte_z": _byte_z,
    "cusum": _cusum,
    "rw_excursion": _rw_excursion,
    "majority_vote": _majority_vote,
    "kmer_mode": _kmer_mode,
}
assert set(_INTEGRATORS) == INTEGRATOR_TYPES  # one source of truth (config.py)
