"""Device-pair block correlation: pure math + a background monitor.

The QPI coherence channel asks one question: do two physically separate
QRNG devices, read simultaneously, show correlated ones-fraction drift?
Under the null (independent devices) they must not — the monitor keeps
a live answer available for the PurityService draw path.

- :func:`block_correlation` is the pure statistic: reduce each side to
  per-block ones-fractions, Pearson-correlate the two series at integer
  block lags in ``[-lag_scan, +lag_scan]``, pick ``r* = argmax |r|``,
  and Fisher-transform it into an approximately standard-normal
  ``z_c = atanh(r*) * sqrt(k_eff - 3)`` where ``k_eff`` is the
  overlapping block count at the chosen lag. Too little overlap
  (``k_eff < max(4, min_valid_blocks)``) makes the evaluation invalid
  (:class:`CoherenceInvalidError`) — never a fake number.

- :class:`CoherenceMonitor` is the repo's second background task (the
  precedent is ``DeviceManager._idle_monitor``): each cycle it reads
  ``blocks_per_side * block_bytes`` from each device of the configured
  pair through the EXACT paired-read choreography the serving path uses
  (sorted-id lock order, one freshness flush per device per evaluation,
  per-chunk monotonic timestamps), computes :func:`block_correlation`,
  and stores the latest :class:`CoherenceValue` behind a plain
  attribute (single event loop — no lock needed). Failures leave the
  last value untouched; staleness is the consumer's check via
  :meth:`CoherenceMonitor.snapshot`, which reports ``valid_now=False``
  once the value is older than ``max_age_s``.

Coherence reads are NOT gate-accounted (no API key; internal
measurement), but every successful evaluation appends one provenance
record with ``protocol: "coherence"`` carrying ``(r, lag, z_c)`` plus
the pair skew — visible in the JSONL like any served request.

Layering: imports ``config`` and ``sources`` only (used by ``server``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass

import numpy as np

from .config import CoherenceConfig
from .sources import SourceRouter

logger = logging.getLogger(__name__)

#: Pearson r is clipped to ±(1 - R_CLIP) before atanh so a numerically
#: perfect correlation maps to a large finite z_c, never infinity.
R_CLIP = 1e-12


class CoherenceInvalidError(ValueError):
    """The evaluation cannot produce a trustworthy statistic (no fake numbers)."""


@dataclass(frozen=True)
class CoherenceValue:
    """One completed coherence evaluation over the device pair."""

    r: float  # Pearson r at the winning lag (r* = argmax |r|)
    lag: int  # winning lag in blocks; positive means B trails A
    z_c: float  # Fisher-transformed statistic, ~N(0,1) under the null
    k_eff: int  # overlapping block count at the winning lag
    max_pair_skew_ns: int  # worst per-chunk read-time skew of this evaluation
    computed_monotonic_ns: int  # staleness basis (time.monotonic_ns at compute)


def block_correlation(
    a: bytes, b: bytes, *, block_bytes: int, lag_scan: int, min_valid_blocks: int
) -> tuple[float, int, float, int]:
    """Lag-scanned block ones-fraction correlation: ``(r*, lag*, z_c, k_eff)``.

    Lag convention: at lag ``L >= 0`` block ``i`` of *a* is paired with
    block ``i + L`` of *b* (a positive lag means B's series trails A's);
    negative lags mirror. ``k_eff`` is the overlap length at the chosen
    lag; ``k_eff < max(4, min_valid_blocks)`` raises
    :class:`CoherenceInvalidError` (Fisher's ``sqrt(k - 3)`` degenerates
    below 4, and the configured floor guards statistical relevance).
    """
    if block_bytes < 1:
        raise ValueError("block_bytes must be >= 1")
    if lag_scan < 0:
        raise ValueError("lag_scan must be >= 0")
    floor = max(4, min_valid_blocks)
    fa = _block_ones_fractions(a, block_bytes)
    fb = _block_ones_fractions(b, block_bytes)
    n = min(len(fa), len(fb))
    if n < floor:
        raise CoherenceInvalidError(
            f"only {n} complete block(s) per side — need at least {floor} "
            f"(max(4, min_valid_blocks={min_valid_blocks}))"
        )
    fa, fb = fa[:n], fb[:n]

    best: tuple[float, float, int, int] | None = None  # (|r|, r, lag, k_eff)
    for lag in range(-lag_scan, lag_scan + 1):
        if lag >= 0:
            x, y = fa[: n - lag], fb[lag:]
        else:
            x, y = fa[-lag:], fb[: n + lag]
        r = _pearson(x, y)
        if r is None:
            continue  # zero-variance segment: r undefined at this lag
        if best is None or abs(r) > best[0]:
            best = (abs(r), r, lag, len(x))
    if best is None:
        raise CoherenceInvalidError(
            "Pearson r undefined at every lag (zero-variance block series)"
        )

    _, r_star, lag_star, k_eff = best
    if k_eff < floor:
        raise CoherenceInvalidError(
            f"k_eff={k_eff} at winning lag {lag_star} is below the validity "
            f"floor {floor} (max(4, min_valid_blocks={min_valid_blocks}))"
        )
    r_clipped = max(min(r_star, 1.0 - R_CLIP), -(1.0 - R_CLIP))
    z_c = math.atanh(r_clipped) * math.sqrt(k_eff - 3)
    return r_star, lag_star, z_c, k_eff


def _block_ones_fractions(data: bytes, block_bytes: int) -> np.ndarray:
    """Reduce *data* to one ones-fraction per complete *block_bytes* block."""
    n_blocks = len(data) // block_bytes
    if n_blocks == 0:
        return np.empty(0, dtype=np.float64)
    buf = np.frombuffer(data, dtype=np.uint8)[: n_blocks * block_bytes]
    bits = np.unpackbits(buf).reshape(n_blocks, block_bytes * 8)
    return bits.mean(axis=1)


def _pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    """Pearson r, or ``None`` when either side has zero variance."""
    if len(x) < 2:
        return None
    xc = x - x.mean()
    yc = y - y.mean()
    denom = math.sqrt(float(np.dot(xc, xc)) * float(np.dot(yc, yc)))
    if denom == 0.0:
        return None
    return float(np.dot(xc, yc)) / denom


class CoherenceMonitor:
    """Background evaluation loop over the configured device pair.

    Started by the server when ``coherence.enabled`` (Step 3 wiring),
    cancelled on shutdown — the ``DeviceManager._idle_monitor`` pattern.
    A failed evaluation (device busy/offline, short read, invalid
    statistic, strict provenance failure) logs a WARNING and leaves the
    last value as-is; the staleness bound in :meth:`snapshot` is what
    downgrades served draws to ``coherence_valid=False``, never a
    fabricated value.
    """

    def __init__(self, config: CoherenceConfig, router: SourceRouter) -> None:
        self._config = config
        self._router = router
        self._latest: CoherenceValue | None = None
        self._task: asyncio.Task | None = None

    @property
    def latest(self) -> CoherenceValue | None:
        return self._latest

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the evaluation loop (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            logger.info(
                "Coherence monitor started (pair=%s, refresh_s=%s)",
                ",".join(self._config.pair),
                self._config.refresh_s,
            )

    async def stop(self) -> None:
        """Cancel the loop and wait for it to unwind."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ── evaluation ─────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            await self.evaluate_once()
            await asyncio.sleep(self._config.refresh_s)

    async def evaluate_once(self) -> CoherenceValue | None:
        """One evaluation cycle; returns the (possibly unchanged) latest value.

        Never raises (except cancellation): the monitor must not die on
        a transient device failure. Provenance is recorded BEFORE the
        value is published so a strict-mode provenance failure keeps the
        no-unrecorded-measurements stance.
        """
        cfg = self._config
        need = cfg.blocks_per_side * cfg.block_bytes
        try:
            (raw_a, raw_b), read = await self._router.coherence_pair_read(cfg.pair, need)
            r, lag, z_c, k_eff = block_correlation(
                raw_a,
                raw_b,
                block_bytes=cfg.block_bytes,
                lag_scan=cfg.lag_scan_blocks,
                min_valid_blocks=cfg.min_valid_blocks,
            )
            value = CoherenceValue(
                r=r,
                lag=lag,
                z_c=z_c,
                k_eff=k_eff,
                max_pair_skew_ns=read.max_pair_skew_ns or 0,
                computed_monotonic_ns=time.monotonic_ns(),
            )
            # NOT gate-accounted (no API key, served_bytes=0) — but every
            # evaluation is a provenance record like any served request.
            self._router.record_provenance(
                read,
                protocol="coherence",
                served_bytes=0,
                extras={"r": r, "lag": lag, "z_c": z_c},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Coherence evaluation failed (last value kept): %s", exc)
            return self._latest
        self._latest = value
        return value

    def snapshot(self) -> tuple[CoherenceValue | None, bool]:
        """``(latest value, valid_now)`` — staleness checked against max_age_s."""
        value = self._latest
        if value is None:
            return None, False
        age_ns = time.monotonic_ns() - value.computed_monotonic_ns
        return value, age_ns <= self._config.max_age_s * 1e9
