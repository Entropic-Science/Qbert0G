#!/usr/bin/env python
"""Record a frozen raw device pool for a ``prng_resample`` control.

NOT part of the server import graph — a standalone operator tool.

The daemon must stay the ONLY reader of the device nodes, so the pool is
recorded THROUGH the running daemon (``qrng.QuantumRNG/GetRandomBytes``,
raw, no conditioning header) with an API key bound to the device the
control must match. Each chunk is a fresh, flushed measurement; chunks
are written in the order measured.

The pool is a snapshot: it carries the device's byte distribution and
within-block correlation as they were while recording. It does not
follow later drift — re-record between study runs if the device has
moved, and note the printed sha256 (the daemon records it in every
provenance fact of the control).

Usage:
    QBERT0G_API_KEY=... python scripts/record_pool.py \\
        --address unix:///run/qbert0g/qbert0g.sock \\
        --bytes 268435456 --out /var/lib/qbert0g/pools/dragonfly-0_pool_v1.bin
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import grpc

from qbert0g.proto import qrng_pb2, qrng_pb2_grpc

DEFAULT_CHUNK = 1_048_576  # one request; must fit the key's per-request cap


def record(
    address: str,
    api_key: str,
    total_bytes: int,
    out: Path,
    *,
    chunk_bytes: int = DEFAULT_CHUNK,
    header: str = "api-key",
    expect_device: str | None = None,
) -> tuple[str, set[str]]:
    """Record *total_bytes* to *out*; returns ``(sha256, serving device ids)``."""
    digest = hashlib.sha256()
    devices: set[str] = set()
    tmp = out.with_suffix(out.suffix + ".partial")
    options = [("grpc.max_receive_message_length", chunk_bytes + 65_536)]
    with grpc.insecure_channel(address, options=options) as channel, open(tmp, "wb") as f:
        stub = qrng_pb2_grpc.QuantumRNGStub(channel)
        remaining = total_bytes
        while remaining:
            n = min(chunk_bytes, remaining)
            response = stub.GetRandomBytes(
                qrng_pb2.RandomRequest(num_bytes=n), metadata=((header, api_key),)
            )
            if len(response.data) != n:
                raise RuntimeError(f"short read: asked {n}, got {len(response.data)}")
            if expect_device is not None and response.device_id != expect_device:
                raise RuntimeError(
                    f"chunk served by {response.device_id!r}, expected {expect_device!r} "
                    "— a pool must come from exactly one device"
                )
            devices.add(response.device_id)
            f.write(response.data)
            digest.update(response.data)
            remaining -= n
    os.replace(tmp, out)
    return digest.hexdigest(), devices


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--address", default="unix:///run/qbert0g/qbert0g.sock")
    parser.add_argument(
        "--bytes", type=int, default=268_435_456, help="pool size (default 256 MiB)"
    )
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device-id", help="refuse chunks served by any other device")
    parser.add_argument("--header", default="api-key")
    args = parser.parse_args(argv)

    api_key = os.environ.get("QBERT0G_API_KEY", "")
    if not api_key:
        sys.exit("set QBERT0G_API_KEY (a key bound to the device to record)")
    if args.out.exists():
        sys.exit(f"{args.out} exists — pools are frozen; record under a new name")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    sha256, devices = record(
        args.address,
        api_key,
        args.bytes,
        args.out,
        chunk_bytes=args.chunk_bytes,
        header=args.header,
        expect_device=args.device_id,
    )
    print(f"pool:    {args.out}")
    print(f"bytes:   {args.bytes}")
    print(f"sha256:  {sha256}")
    print(f"device:  {', '.join(sorted(devices))}")
    print(f"seconds: {time.time() - started:.1f}")


if __name__ == "__main__":
    main()
