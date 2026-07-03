#!/usr/bin/env python
"""Fit an order-1 byte Markov model from raw QRNG dumps for ``prng_markov``.

NOT part of the server import graph — a standalone fitting tool.

Input: one or more raw ``.bin`` dumps from the device the control must
emulate. Output: an npz file with:

- ``initial``    float64[256]      — smoothed empirical byte distribution
- ``transition`` float64[256,256]  — row-stochastic, Laplace +1 smoothed
- ``meta``       JSON string       — source device id, firmware version,
  session id, dump SHA-256 list, fit date, smoothing constant

Prints a fingerprint summary (byte mean, per-bit-position P(1), lag-1
byte correlation) computed from the fitted model AND from the dumps,
side by side, so a human can eyeball the fit before use.

Usage:
    python scripts/fit_markov.py --device-id dragonfly-0 \\
        --out models/dragonfly-0_v1.npz dump1.bin [dump2.bin ...]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path

import numpy as np

DEFAULT_SMOOTHING = 1.0  # Laplace +1


def fit(dumps: list[bytes], smoothing: float = DEFAULT_SMOOTHING) -> tuple[np.ndarray, np.ndarray]:
    """Fit ``(initial, transition)`` from raw dumps with Laplace smoothing.

    Transitions are counted within each dump only (never across dump
    boundaries — consecutive files are not consecutive measurements).
    """
    byte_counts = np.zeros(256, dtype=np.float64)
    transition_counts = np.zeros((256, 256), dtype=np.float64)
    for data in dumps:
        arr = np.frombuffer(data, dtype=np.uint8)
        byte_counts += np.bincount(arr, minlength=256)
        if len(arr) > 1:
            pair_idx = arr[:-1].astype(np.intp) * 256 + arr[1:]
            transition_counts += np.bincount(pair_idx, minlength=65536).reshape(256, 256)
    initial = (byte_counts + smoothing) / (byte_counts.sum() + 256 * smoothing)
    row_totals = transition_counts.sum(axis=1, keepdims=True)
    transition = (transition_counts + smoothing) / (row_totals + 256 * smoothing)
    return initial, transition


def fingerprint_from_dumps(dumps: list[bytes]) -> dict[str, object]:
    """Empirical fingerprint: byte mean, per-bit P(1), lag-1 byte correlation."""
    arr = np.frombuffer(b"".join(dumps), dtype=np.uint8)
    bits = np.unpackbits(arr).reshape(-1, 8)
    lag1 = float(np.corrcoef(arr[:-1], arr[1:])[0, 1]) if len(arr) > 1 else float("nan")
    return {
        "byte_mean": float(arr.mean()),
        "bit_p1": [float(p) for p in bits.mean(axis=0)],
        "lag1_corr": lag1,
    }


def fingerprint_from_model(initial: np.ndarray, transition: np.ndarray) -> dict[str, object]:
    """Analytic fingerprint of the fitted model (initial as stationary)."""
    values = np.arange(256, dtype=np.float64)
    mean = float(initial @ values)
    var = float(initial @ (values - mean) ** 2)
    # E[X_t * X_{t+1}] under (initial, transition):
    exy = float(np.einsum("x,x,xy,y->", initial, values, transition, values))
    bit_masks = ((np.arange(256)[:, None] >> np.arange(7, -1, -1)[None, :]) & 1).astype(
        np.float64
    )
    return {
        "byte_mean": mean,
        "bit_p1": [float(p) for p in initial @ bit_masks],
        "lag1_corr": (exy - mean**2) / var if var > 0 else float("nan"),
    }


def print_fingerprints(model_fp: dict, dump_fp: dict) -> None:
    """Side-by-side model-vs-dump summary for human eyeballing."""
    print(f"{'fingerprint':<24}{'model':>14}{'dumps':>14}")
    print("-" * 52)
    print(f"{'byte mean':<24}{model_fp['byte_mean']:>14.4f}{dump_fp['byte_mean']:>14.4f}")
    for i, (m, d) in enumerate(zip(model_fp["bit_p1"], dump_fp["bit_p1"], strict=True)):
        print(f"{f'P(1) bit {i} (MSB first)':<24}{m:>14.6f}{d:>14.6f}")
    print(f"{'lag-1 byte corr':<24}{model_fp['lag1_corr']:>14.6f}{dump_fp['lag1_corr']:>14.6f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fit a prng_markov npz model from raw QRNG dumps."
    )
    parser.add_argument("dumps", nargs="+", help="Raw .bin dump file(s)")
    parser.add_argument("--out", required=True, help="Output npz path")
    parser.add_argument("--device-id", required=True, help="Source device id (meta)")
    parser.add_argument("--firmware", default="", help="Device firmware version (meta)")
    parser.add_argument("--session", default="", help="Measurement session id (meta)")
    parser.add_argument(
        "--smoothing",
        type=float,
        default=DEFAULT_SMOOTHING,
        help=f"Laplace smoothing constant (default: {DEFAULT_SMOOTHING})",
    )
    args = parser.parse_args(argv)

    dump_paths = [Path(p) for p in args.dumps]
    dumps = [p.read_bytes() for p in dump_paths]
    total = sum(len(d) for d in dumps)
    print(f"Fitting on {len(dumps)} dump(s), {total} bytes total.")

    initial, transition = fit(dumps, smoothing=args.smoothing)
    meta = {
        "source_device_id": args.device_id,
        "firmware_version": args.firmware,
        "session_id": args.session,
        "dump_sha256": [hashlib.sha256(d).hexdigest() for d in dumps],
        "fit_date": datetime.datetime.now(datetime.UTC).date().isoformat(),
        "laplace_smoothing": args.smoothing,
    }
    np.savez(args.out, initial=initial, transition=transition, meta=json.dumps(meta))
    print(f"Model written to {args.out}\n")
    print_fingerprints(fingerprint_from_model(initial, transition), fingerprint_from_dumps(dumps))


if __name__ == "__main__":
    main()
