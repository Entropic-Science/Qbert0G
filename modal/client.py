"""
Modal client for the QRNG gRPC service.

Uses cloudflared access tcp as a per-container sidecar to reach the
home-hosted QRNG service through Cloudflare Access — no WARP, no TUN device,
no kernel capabilities required.

Prerequisites
-------------
1. Cloudflare dashboard (Zero Trust):
   a. Access → Applications → Add a Self-hosted TCP Application
        Hostname : qrng.your-domain.com          # REPLACE
        Origin   : localhost:50051 (via your existing cloudflared tunnel)
        Policy   : Service Auth only (no email OTP)
   b. Access → Service Auth → Create Service Token
        Copy the Client ID and Client Secret

2. Modal secrets:
     modal secret create cf-access-qrng \\
         CF_ACCESS_CLIENT_ID=<id> \\
         CF_ACCESS_CLIENT_SECRET=<secret> \\
         QRNG_TUNNEL_HOSTNAME=qrng.your-domain.com   # REPLACE

     modal secret create qrng-api-key QRNG_API_KEY=<key>

3. Run:
     modal run modal/client.py
     modal run modal/client.py --n 64   # request more bytes
"""

from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------

# Resolved at image-build time; works as long as this file lives in modal/
# inside the qrng-grpc repo root.
_PROTO_FILE = Path(__file__).parent / "proto" / "qrng.proto"

image = (
    modal.Image.debian_slim()
    .apt_install("curl")
    .run_commands(
        # cloudflared is a single static binary that opens a local TCP listener
        # and proxies it through Cloudflare's edge to the tunnel origin.
        # Outbound HTTPS only — no TUN device, no NET_ADMIN capability.
        "curl -fsSL -o /tmp/cloudflared.deb "
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb "
        "&& dpkg -i /tmp/cloudflared.deb",
        "mkdir -p /app/proto && touch /app/proto/__init__.py",
    )
    .pip_install("grpcio>=1.60.0", "grpcio-tools>=1.60.0", "protobuf>=4.25.0")
    # Generate stubs at build time from the authoritative .proto definition.
    # Generating here (rather than copying pre-compiled stubs) ensures the
    # stubs match whichever grpcio version gets installed.
    .add_local_file(str(_PROTO_FILE), "/app/proto/qrng.proto", copy=True)
    .run_commands(
        "python -m grpc_tools.protoc -I /app/proto "
        "--python_out=/app/proto --grpc_python_out=/app/proto "
        "/app/proto/qrng.proto"
    )
    .env({"PYTHONPATH": "/app:/app/proto"})
)

app = modal.App("qrng-client", image=image)

# ---------------------------------------------------------------------------
# Client class
# ---------------------------------------------------------------------------


@app.cls(
    secrets=[
        modal.Secret.from_name("cf-access-qrng"),  # CF_ACCESS_CLIENT_ID, CF_ACCESS_CLIENT_SECRET, QRNG_TUNNEL_HOSTNAME
        modal.Secret.from_name("qrng-api-key"),     # QRNG_API_KEY
    ],
)
class QRNGClient:
    """gRPC client with a persistent cloudflared tunnel sidecar.

    The tunnel starts once per container in start_tunnel() and is reused for
    every get_random_bytes() call. Keeping it persistent avoids ~1.5s
    cloudflared startup overhead on every request — important on hot paths
    such as a vLLM sampler.
    """

    @modal.enter()
    def start_tunnel(self) -> None:
        import grpc
        import os

        self._tunnel = subprocess.Popen(
            [
                "cloudflared", "access", "tcp",
                "--hostname", os.environ["QRNG_TUNNEL_HOSTNAME"],
                "--url", "127.0.0.1:50051",
                "--service-token-id", os.environ["CF_ACCESS_CLIENT_ID"],
                "--service-token-secret", os.environ["CF_ACCESS_CLIENT_SECRET"],
            ],
        )
        _wait_for_listener()
        # Keep the channel open across calls — avoids per-call connection
        # setup overhead (TCP handshake + HTTP/2 negotiation).
        self._channel = grpc.insecure_channel("127.0.0.1:50051")
        # insecure_channel is safe here: cloudflared already wraps the
        # container→Cloudflare leg in TLS; this channel is loopback-only.

    @modal.exit()
    def stop_tunnel(self) -> None:
        self._channel.close()
        self._tunnel.terminate()
        try:
            self._tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._tunnel.kill()

    @modal.method()
    def get_random_bytes(self, n: int) -> bytes:
        import os
        from proto import qrng_pb2, qrng_pb2_grpc

        stub = qrng_pb2_grpc.QuantumRNGStub(self._channel)
        response = stub.GetRandomBytes(
            qrng_pb2.RandomRequest(num_bytes=n),
            metadata=(("api-key", os.environ["QRNG_API_KEY"]),),
        )
        return bytes(response.data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_listener(host: str = "127.0.0.1", port: int = 50051, timeout: float = 10.0) -> None:
    """Block until cloudflared's local TCP listener accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(
        f"cloudflared did not become ready on {host}:{port} within {timeout}s — "
        "check CF_ACCESS_CLIENT_ID, CF_ACCESS_CLIENT_SECRET, and QRNG_TUNNEL_HOSTNAME"
    )


# ---------------------------------------------------------------------------
# Local entrypoint:  modal run modal/client.py [--n 32]
# ---------------------------------------------------------------------------


@app.local_entrypoint()
def main(n: int = 32) -> None:
    client = QRNGClient()
    data = client.get_random_bytes.remote(n)
    print(f"Received {len(data)} quantum random bytes:")
    print(data.hex())
