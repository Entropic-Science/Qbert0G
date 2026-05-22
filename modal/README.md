# QRNG Modal Client

Calls the QRNG gRPC service from Modal workers via a `cloudflared` sidecar.
No WARP, no VPN — just a static binary and two secrets.

## Prerequisites

- A [Modal](https://modal.com) account
- The credentials provided to you out-of-band:
  - `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`
  - `QRNG_API_KEY`

## Setup

**1. Install Modal**

```bash
pip install modal
modal setup
```

`modal setup` opens a browser to authenticate your workspace.

**2. Create secrets**

```bash
modal secret create cf-access-qrng \
    CF_ACCESS_CLIENT_ID="<provided>" \
    CF_ACCESS_CLIENT_SECRET="<provided>" \
    QRNG_TUNNEL_HOSTNAME="qbert-grpc.cipherstone.co"

modal secret create qrng-api-key \
    QRNG_API_KEY="<provided>"
```

**3. Package layout**

Ensure your directory looks like this before running:

```
qrng-modal/
  client.py
  proto/
    qrng.proto
  README.md
```

## Usage

**One-shot from the terminal**

```bash
modal run client.py           # 32 bytes (default)
modal run client.py --n 256   # request more bytes.  Your key currently supports max of 13312 per request
```

Output:

```
Received 256 quantum random bytes:
3fa2c1...
```

**From another Modal function**

```python
from client import QRNGClient

@app.function()
def my_function():
    qrng = QRNGClient()
    data = qrng.get_random_bytes.remote(64)
```

The tunnel sidecar starts once per container and stays alive across calls —
there is no per-call connection overhead.

## Byte limits of Alchy's key (these can be adjusted by request)

| Limit | Default |
|---|---|
| Per request | 13,312 bytes |
| Per minute | 500 requests |
| Per day | 500 MB |

Requests exceeding these return a gRPC `RESOURCE_EXHAUSTED` status.
