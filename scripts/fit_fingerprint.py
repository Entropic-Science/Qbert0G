#!/usr/bin/env python
"""Fit a per-source statistical fingerprint JSON from raw QRNG dumps.

NOT part of the server import graph — a standalone fitting tool
(the fit_markov.py convention). Input: one or more raw ``.bin`` dumps
from the source to characterize. Output: the fingerprint JSON consumed
by ``qbert0g`` (config ``devices[].fingerprint`` / ``controls[].
fingerprint``):

- ``ones_fraction``          P(bit = 1) over all dump bits
- ``byte_mean`` / ``byte_std``  uint8 mean and population std
- ``bit_acf``                bit autocorrelation at lags {1, 2, 3, 4, 8}
- ``neff_factor``            1 / (1 + 2 * sum(rho_k))
- provenance metadata        device id, firmware, session ref, dump
  SHA-256 list, fit timestamp, quantum-fraction tier

ACF is never computed across dump boundaries (consecutive files are
not consecutive measurements). Prints a side-by-side dump-vs-stored
comparison so a human can eyeball the fit before signing it off.

Usage:
    python scripts/fit_fingerprint.py --device-id dragonfly-0 \\
        --out fingerprints/dragonfly-0_v1.json dump1.bin [dump2.bin ...]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path

import numpy as np

#: Fixed ACF lag set (bit lags) baked into every fingerprint.
ACF_LAGS = (1, 2, 3, 4, 8)

#: Quantum-fraction tiers (mirrors qbert0g.config.QF_TIERS; the script
#: stays import-free of the server package on purpose).
QF_TIERS = ("99+", "98+", "95+", "90+", "unrated")


def fit(dumps: list[bytes]) -> dict[str, object]:
    """Compute the fingerprint statistics from raw dumps.

    Bit ACF at lag k pools per-dump numerators/denominators (centered
    on the global bit mean) so no lagged product crosses a dump
    boundary: ``rho_k = sum_d <x_d[:-k], x_d[k:]> / sum_d <x_d, x_d>``.
    """
    arr = np.frombuffer(b"".join(dumps), dtype=np.uint8)
    if arr.size == 0:
        raise SystemExit("fit_fingerprint: dumps are empty")
    all_bits = np.unpackbits(arr)
    ones_fraction = float(all_bits.mean())

    numerators = dict.fromkeys(ACF_LAGS, 0.0)
    denominator = 0.0
    for dump in dumps:
        bits = np.unpackbits(np.frombuffer(dump, dtype=np.uint8)).astype(np.float64)
        x = bits - ones_fraction
        denominator += float(np.dot(x, x))
        for lag in ACF_LAGS:
            if len(x) > lag:
                numerators[lag] += float(np.dot(x[:-lag], x[lag:]))
    if denominator <= 0.0:
        raise SystemExit("fit_fingerprint: degenerate dumps (zero bit variance)")
    bit_acf = {lag: numerators[lag] / denominator for lag in ACF_LAGS}

    acf_sum = sum(bit_acf.values())
    neff_denominator = 1.0 + 2.0 * acf_sum
    if neff_denominator <= 0.0:
        raise SystemExit(
            f"fit_fingerprint: bit ACF sum {acf_sum:.6f} leaves no positive effective "
            "sample size (1 + 2*sum(rho) <= 0) — this fingerprint would be refused"
        )
    return {
        "ones_fraction": ones_fraction,
        "byte_mean": float(arr.mean()),
        "byte_std": float(arr.std()),
        "bit_acf": bit_acf,
        "neff_factor": 1.0 / neff_denominator,
    }


def print_comparison(dump_stats: dict, stored: dict) -> None:
    """Side-by-side dump-vs-stored summary for human sign-off."""
    stored_acf = {int(k): v for k, v in stored["bit_acf"].items()}
    print(f"{'fingerprint':<24}{'dumps':>16}{'stored':>16}")
    print("-" * 56)
    print(
        f"{'ones fraction':<24}"
        f"{dump_stats['ones_fraction']:>16.8f}{stored['ones_fraction']:>16.8f}"
    )
    print(f"{'byte mean':<24}{dump_stats['byte_mean']:>16.4f}{stored['byte_mean']:>16.4f}")
    print(f"{'byte std':<24}{dump_stats['byte_std']:>16.4f}{stored['byte_std']:>16.4f}")
    for lag in ACF_LAGS:
        print(
            f"{f'bit ACF lag {lag}':<24}"
            f"{dump_stats['bit_acf'][lag]:>16.8f}{stored_acf[lag]:>16.8f}"
        )
    print(f"{'neff factor':<24}{dump_stats['neff_factor']:>16.8f}{stored['neff_factor']:>16.8f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fit a per-source fingerprint JSON from raw QRNG dumps."
    )
    parser.add_argument("dumps", nargs="+", help="Raw .bin dump file(s)")
    parser.add_argument("--out", required=True, help="Output fingerprint JSON path")
    parser.add_argument("--device-id", required=True, help="Source device id (meta)")
    parser.add_argument("--firmware", default="", help="Device firmware version (meta)")
    parser.add_argument("--session", default="", help="Measurement session id (meta)")
    parser.add_argument(
        "--tier",
        default="unrated",
        choices=QF_TIERS,
        help="Quantum-fraction tier from a characterization (default: unrated)",
    )
    args = parser.parse_args(argv)

    dump_paths = [Path(p) for p in args.dumps]
    dumps = [p.read_bytes() for p in dump_paths]
    total = sum(len(d) for d in dumps)
    print(f"Fitting on {len(dumps)} dump(s), {total} bytes total.")

    stats = fit(dumps)
    fingerprint = {
        "device_id": args.device_id,
        "firmware": args.firmware,
        "ones_fraction": stats["ones_fraction"],
        "byte_mean": stats["byte_mean"],
        "byte_std": stats["byte_std"],
        "bit_acf": {str(lag): rho for lag, rho in stats["bit_acf"].items()},
        "neff_factor": stats["neff_factor"],
        "quantum_fraction_tier": args.tier,
        "source_dumps_sha256": [hashlib.sha256(d).hexdigest() for d in dumps],
        "fitted_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "session_ref": args.session,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(fingerprint, indent=2) + "\n", encoding="utf-8")
    print(f"Fingerprint written to {out_path}\n")
    stored = json.loads(out_path.read_text(encoding="utf-8"))
    print_comparison(stats, stored)


if __name__ == "__main__":
    main()
