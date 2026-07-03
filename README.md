# Qbert0G

A gRPC service that streams **freshly measured quantum noise** from Crypta Labs QRNG devices (Firefly, QCicada, Dragonfly). Built for entropy research.

> **Not for cryptographic or security use.**
> Although Crypta Labs devices support cryptographic use cases, the output of this particular service isn't intended for such. If you need a cryptographic RNG service, look elsewhere.

## Features

- **Fresh and exclusive data per request** — no pooling, buffering, or pre-generation; every byte is measured upon request and served exclusively to the requester. When `freshness.flush_device_buffer` is on (the default), the serial receive buffer is flushed immediately before **every** measurement, so no byte measured before the request is ever served.
- **Two wire protocols on one server** — the public `qrng.QuantumRNG` service for general clients, plus the `qr_entropy.EntropyService` protocol consumed natively by [qr-sampler](https://github.com/Entropic-Science) (sequence-id echo, nanosecond generation timestamps, bidirectional streaming).
- **Low latency and efficient on the wire** — gRPC over HTTP/2; optional UNIX-domain-socket binding for co-located clients.
- **Per-request provenance** — responses carry `device_id` and a measurement timestamp so samples can be attributed and reproduced in datasets.
- **Selectable post-processing** — `raw` (zero post-processing), `sha256`, or `raw_samples`, globally or per device.
- **Device failover** — automatic fallback across configured devices.
- **API key management** — per-key rate limits, daily byte caps, and per-request byte caps, tracked in SQLite; managed via `qbert0g keys`.
- **Mock device type** — run the full server without hardware (`os.urandom`, loudly labelled NOT quantum) for development and CI.

## Architecture

```mermaid
flowchart TD
    A["General clients<br/>(any language)"] -- "qrng.QuantumRNG/GetRandomBytes" --> S["gRPC server<br/>(TCP and/or UNIX socket)"]
    B["qr-sampler / qthought<br/>(QuantumGrpcSource)"] -- "qr_entropy.EntropyService<br/>GetEntropy + StreamEntropy" --> S
    S --> G["RequestGate<br/>(auth, rate limits, byte caps, usage)"]
    G --> D["DeviceManager<br/>(locking, failover, freshness flush)"]
    D --> Q1["Firefly / QCicada / Dragonfly<br/>(pyqcc)"]
    D --> Q2["mock (os.urandom, dev only)"]
    G --> DB[("SQLite<br/>API keys + usage")]
```

Both services share one request pipeline: auth, rate limiting, byte caps, device routing and usage accounting are identical regardless of which protocol a client speaks.

## Quick start

```bash
git clone https://github.com/Entropic-Science/Qbert0G
cd Qbert0G
pip install .

# Hardware devices additionally need pyqcc (wheel from Crypta Labs):
# pip install /path/to/pyqcc-x.y.z-py3-none-any.whl

cp config.yaml.example config.yaml
#   edit config.yaml: set auth.api_key, configure devices

qbert0g check-config          # validate before starting
qbert0g serve                 # run the server
```

Config resolution everywhere: `--config PATH` > `QBERT0G_CONFIG` env var > `./config.yaml`. A missing config file is an error — the daemon never starts on silent defaults.

To try it without hardware, configure a mock device:

```yaml
devices:
  - id: "mock-0"
    type: "mock"     # os.urandom — NOT quantum; development only
```

## The two protocols

### `qrng.QuantumRNG` — the public service

**Request (`RandomRequest`):** `num_bytes` (uint32). API key via gRPC metadata (default header `api-key`).

**Response (`RandomResponse`):** `data` (bytes), `timestamp` (uint64, epoch **microseconds**, stamped at measurement time), `device_id` (string).

```python
import grpc
from qbert0g.proto import qrng_pb2, qrng_pb2_grpc

channel = grpc.insecure_channel("localhost:50051")
stub = qrng_pb2_grpc.QuantumRNGStub(channel)
response = stub.GetRandomBytes(
    qrng_pb2.RandomRequest(num_bytes=100),
    metadata=[("api-key", "your-api-key-here")],
)
print(len(response.data), response.device_id, response.timestamp)
```

Or with grpcurl:

```bash
grpcurl -plaintext \
  -proto src/qbert0g/proto/qrng.proto \
  -H 'api-key: YOUR_API_KEY' \
  -d '{"num_bytes": 1024}' \
  localhost:50051 qrng.QuantumRNG/GetRandomBytes
```

### `qr_entropy.EntropyService` — the qr-sampler seam

**Request (`EntropyRequest`):** `bytes_needed` (int32), `sequence_id` (int64 — qr-sampler sends a 63-bit commitment nonce here on its pipelined path).

**Response (`EntropyResponse`):** `data`, `sequence_id` (echoed verbatim — this is what lets qr-sampler verify post-selection ordering, `echo_verified`), `generation_timestamp_ns` (epoch nanoseconds at measurement), `device_id`.

RPCs: `GetEntropy` (unary) and `StreamEntropy` (bidirectional stream — one response per in-stream request, each passing the full auth/limits gate). The bidi RPC is what unlocks qr-sampler's lowest-latency `bidi_streaming` mode.

A qr-sampler client needs **zero protocol configuration** — its defaults (`/qr_entropy.EntropyService/GetEntropy` + `StreamEntropy`) resolve against this server directly:

```python
from qr_sampler.contract import QRSamplerConfig
from qr_sampler.entropy.qgrpc.source import QuantumGrpcSource

source = QuantumGrpcSource(QRSamplerConfig(
    grpc_server_address="127.0.0.1:50051",
    grpc_api_key="your-api-key-here",
))
data = source.get_random_bytes(10_000)
```

See `examples/client.py` for a runnable demo of both protocols, and `tests/test_qr_sampler_seam.py` for the full cross-repo contract (echo verification, bidi streaming, legacy path).

## API key management

The bootstrap admin key comes from `auth.api_key` in `config.yaml`: on first startup an enabled admin key (device `*`) is created for it. Changing the value later **adds** a second admin; revoke old keys explicitly.

All key management goes through the CLI (run on the server box; `--config` locates the database):

```bash
qbert0g keys list
qbert0g keys create --name "my-client" --device firefly-1
qbert0g keys create --name "high-volume" --device dragonfly-0 \
    --rate-limit 500 --daily-bytes 524288000 --max-bytes 65536
qbert0g keys create --name "ops-admin" --device "*" --admin
qbert0g keys update --id <key-id> --rate-limit 100
qbert0g keys disable --id <key-id>
qbert0g keys enable  --id <key-id>
qbert0g keys usage   --id <key-id> --days 30
qbert0g keys delete  --id <key-id>          # add --yes to skip the prompt
```

The raw key is printed once at creation and cannot be retrieved again (only its SHA-256 hash is stored).

**Per-key limits** (omit to use the service-wide default from `limits:`):

| Flag | Description |
|------|-------------|
| `--rate-limit RPM` | Max requests per minute |
| `--daily-bytes BYTES` | Max bytes served per day |
| `--max-bytes BYTES` | Max bytes per individual request |

## Configuration

See `config.yaml.example` for the full annotated schema. Highlights:

```yaml
server:
  listen: "127.0.0.1:50051"    # TCP bind; 0.0.0.0 exposes entropy off-box (warned)
  unix_socket: ""              # preferred transport for co-located clients
  request_timeout: 5.0
  failover_enabled: true
database:
  path: "./qbert0g.db"
auth:
  api_key: "..."               # bootstrap admin key
  header: "api-key"
limits:
  max_bytes_per_request: 16384
  max_bytes_per_day: 104857600
  rate_limit_per_minute: 200
post_processing:
  mode: raw                    # raw | sha256 | raw_samples
freshness:
  flush_device_buffer: true    # flush serial RX buffer before EVERY read
  emit_generation_timestamp: true
  allow_pooling: false         # declarative guard — true is refused
  allow_pregeneration: false
devices:
  - id: "dragonfly-0"
    type: "dragonfly"          # firefly | qcicada | dragonfly | mock
    path: "/dev/ttyQRNG0"
    streaming_mode: true
```

Validation is strict: **unknown keys are rejected at startup**, so a typo can never silently change what kind of randomness is served.

## Error handling

| gRPC Status | Reason |
|-------------|--------|
| `UNAUTHENTICATED` | Missing or invalid API key |
| `RESOURCE_EXHAUSTED` | Rate limit or daily byte limit exceeded |
| `INVALID_ARGUMENT` | Byte count is 0 or exceeds the per-request limit |
| `UNAVAILABLE` | No devices available or device error |
| `DEADLINE_EXCEEDED` | Request timed out waiting for a device |

## Development

```bash
pip install -e .[dev]
make test          # pytest (46 tests; hardware not required — mock device)
make check         # ruff + pytest
make proto         # regenerate protobuf stubs after editing .proto files
```

The cross-repo seam tests (`tests/test_qr_sampler_seam.py`) run automatically when qr-sampler is importable (`pip install -e ../qr-sampler`) and skip otherwise.

### Project structure

```
Qbert0G/
├── src/qbert0g/
│   ├── cli.py           # qbert0g serve | keys | check-config
│   ├── config.py        # strict YAML schema (unknown keys rejected)
│   ├── database.py      # API keys + usage tracking (SQLite, hashed keys)
│   ├── devices.py       # DeviceManager: pyqcc drivers + mock, flush, failover
│   ├── gate.py          # shared request pipeline (auth, limits, measure)
│   ├── server.py        # both gRPC servicers + QbertServer lifecycle
│   └── proto/           # qrng.proto + entropy_service.proto + generated stubs
├── tests/               # config, database, devices, server, qr-sampler seam
├── examples/client.py   # both protocols, runnable
└── config.yaml.example  # annotated canonical config
```

## Troubleshooting

- **`Config error: ... unknown key(s)`** — your config uses the pre-1.0 schema (`service:`, `admin_api_key`, integer `post_processing_mode`). Migrate to the schema in `config.yaml.example`; see `CHANGELOG.md`.
- **`pyqcc not available`** — install the wheel from Crypta Labs: `pip install /path/to/pyqcc-x.y.z-py3-none-any.whl`. Mock devices work without it.
- **Port already in use** — change `server.listen` in `config.yaml`.
- **Device permission denied** — add the service user to the `dialout` group.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).

```
Copyright 2026 Entropic Science, Bradley Stephenson (orphiceye)
```
