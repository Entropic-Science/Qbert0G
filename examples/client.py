#!/usr/bin/env python3
"""Minimal clients for both Qbert0G wire protocols.

Usage:
    python examples/client.py --api-key YOUR_KEY [--address 127.0.0.1:50051] [-n 64]

Demonstrates:
- ``qrng.QuantumRNG/GetRandomBytes``      (the public protocol)
- ``qr_entropy.EntropyService/GetEntropy`` (the qr-sampler protocol)
"""

from __future__ import annotations

import argparse

import grpc

from qbert0g.proto import (
    entropy_service_pb2,
    entropy_service_pb2_grpc,
    qrng_pb2,
    qrng_pb2_grpc,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="127.0.0.1:50051")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("-n", "--num-bytes", type=int, default=32)
    args = parser.parse_args()

    metadata = [("api-key", args.api_key)]
    with grpc.insecure_channel(args.address) as channel:
        # ── public protocol ──────────────────────────────────────────
        qrng_stub = qrng_pb2_grpc.QuantumRNGStub(channel)
        response = qrng_stub.GetRandomBytes(
            qrng_pb2.RandomRequest(num_bytes=args.num_bytes), metadata=metadata
        )
        print(f"[QuantumRNG]     {len(response.data)} bytes "
              f"from {response.device_id} at t={response.timestamp}us")
        print(f"                 {response.data.hex()}")

        # ── qr-sampler protocol ──────────────────────────────────────
        entropy_stub = entropy_service_pb2_grpc.EntropyServiceStub(channel)
        reply = entropy_stub.GetEntropy(
            entropy_service_pb2.EntropyRequest(bytes_needed=args.num_bytes, sequence_id=42),
            metadata=metadata,
        )
        print(f"[EntropyService] {len(reply.data)} bytes "
              f"from {reply.device_id} at t={reply.generation_timestamp_ns}ns "
              f"(sequence_id echo: {reply.sequence_id})")
        print(f"                 {reply.data.hex()}")


if __name__ == "__main__":
    main()
