#!/usr/bin/env python
"""Offline: what does a raw dump look like to a z-score sampler, per mode?

NOT part of the server import graph — a standalone analysis tool.

Takes a raw device dump and reports, for each conditioning mode, the
statistics of the per-block draw a byte-mean z-score consumer computes
(``z = (mean - 127.5) / (sd_uniform / sqrt(n))``, ``u = Phi(z)``):

- ``raw``            — the dump as measured;
- ``hash_truncate``  — the dump through the normative transform in
  ``qbert0g.profiles`` with the given parameters;
- ``resample``       — (with ``--resample-seed``) seeded-PRNG-chosen
  windows of the dump, the ``prng_resample`` control's view of it.

A diagnostic for humans choosing parameters. It is NOT a pass criterion
for served output (AGENTS.md invariant 8): matched controls are
legitimately non-uniform.

Usage:
    python scripts/conditioning_check.py dump.bin
    python scripts/conditioning_check.py dump.bin --hash sha512 --in-bytes 256 --keep-bytes 32
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np

from qbert0g.controls import resample_window_offset
from qbert0g.profiles import hash_truncate, hash_truncate_bytes_needed

FAIR_MEAN = 127.5
FAIR_SD = math.sqrt((256**2 - 1) / 12)  # discrete uniform on 0..255


def block_draws(data: bytes, block_bytes: int) -> tuple[np.ndarray, np.ndarray]:
    """``(z, u)`` per consecutive *block_bytes* block of *data*."""
    arr = np.frombuffer(data, dtype=np.uint8)
    blocks = len(arr) // block_bytes
    means = arr[: blocks * block_bytes].reshape(blocks, block_bytes).mean(axis=1)
    z = (means - FAIR_MEAN) / (FAIR_SD / math.sqrt(block_bytes))
    u = np.array([0.5 * (1.0 + math.erf(v / math.sqrt(2.0))) for v in z])
    return z, u


def ks_uniform(u: np.ndarray) -> float:
    """Kolmogorov-Smirnov distance of *u* from Uniform(0, 1)."""
    s = np.sort(u)
    n = len(s)
    if n == 0:
        return float("nan")
    grid = np.arange(1, n + 1) / n
    return float(max(np.max(grid - s), np.max(s - (grid - 1.0 / n))))


def summarize(label: str, data: bytes, block_bytes: int) -> dict[str, float | str]:
    arr = np.frombuffer(data, dtype=np.uint8)
    z, u = block_draws(data, block_bytes)
    return {
        "mode": label,
        "bytes": len(arr),
        "byte_mean": float(arr.mean()),
        "byte_sd": float(arr.std()),
        "blocks": len(z),
        "z_mean": float(z.mean()) if len(z) else float("nan"),
        "z_sd": float(z.std(ddof=1)) if len(z) > 1 else float("nan"),
        "u_mean": float(u.mean()) if len(u) else float("nan"),
        "u_low_decile": float((u < 0.1).mean()) if len(u) else float("nan"),
        "u_high_decile": float((u > 0.9).mean()) if len(u) else float("nan"),
        "ks": ks_uniform(u),
    }


def resampled(data: bytes, seed: int, block_bytes: int, draws: int) -> bytes:
    """*draws* PRNG-chosen windows of *block_bytes*, concatenated."""
    out = bytearray()
    for i in range(draws):
        offset = resample_window_offset(seed, i, len(data), block_bytes)
        out += data[offset : offset + block_bytes]
    return bytes(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dump", type=Path)
    parser.add_argument("--block-bytes", type=int, default=10_000, help="bytes per draw")
    parser.add_argument("--hash", default="sha256")
    parser.add_argument("--in-bytes", type=int, default=128)
    parser.add_argument("--keep-bytes", type=int, default=16)
    parser.add_argument("--resample-seed", help="128-bit hex seed; adds the resample row")
    args = parser.parse_args(argv)

    raw = args.dump.read_bytes()
    rows = [summarize("raw", raw, args.block_bytes)]

    out_bytes = (len(raw) // args.in_bytes) * args.keep_bytes
    need = hash_truncate_bytes_needed(out_bytes, args.in_bytes, args.keep_bytes)
    hashed = hash_truncate(raw[:need], out_bytes, args.hash, args.in_bytes, args.keep_bytes)
    rows.append(
        summarize(
            f"hash_truncate;{args.hash};{args.in_bytes};{args.keep_bytes}",
            hashed,
            args.block_bytes,
        )
    )
    if args.resample_seed:
        draws = len(raw) // args.block_bytes
        rows.append(
            summarize(
                "resample",
                resampled(raw, int(args.resample_seed, 16), args.block_bytes, draws),
                args.block_bytes,
            )
        )

    keys = list(rows[0])
    for key in keys:
        cells = [
            f"{row[key]:.4f}" if isinstance(row[key], float) else str(row[key]) for row in rows
        ]
        print(f"{key:>14}  " + "  ".join(f"{c:>32}" for c in cells))


if __name__ == "__main__":
    main()
