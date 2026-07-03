# Changelog

## Unreleased

### Added

- **`chardev` device type** — PCIe Dragonfly cards exposed as plain
  character devices (`/dev/qrngDF*`). No pyqcc, no qcc-cli `-P` chain:
  the server serves whatever the driver DMA delivers (`post_processing`
  is rejected on this type), with no one-shot size limit. Optional
  `pci_address` on the device entry enables, via sysfs:
  - **freshness translation** of the serial flush contract: before every
    measurement, `ready_count` (32-bit words) is read and
    `ready_count * 4` buffered bytes are drained (tracked per measurement
    as `last_flushed_bytes` for provenance); without `pci_address` a
    one-time warning marks the device as flush-unavailable;
  - **health snapshot** per measurement: `error_present` / `error_bits`
    (clear-on-read — Qbert0G must be the only reader on the box), exposed
    through `get_device_status`.

## 1.0.0 — 2026-07-03

Breaking upgrade: Qbert0G becomes an installable package that natively serves
qr-sampler's wire protocol alongside its public one, and its configuration is
aligned with the qthought deployment contract. **No backward compatibility is
kept** with the pre-1.0 layout or config schema.

### Added

- **`qr_entropy.EntropyService`** registered on the same server as
  `qrng.QuantumRNG`: `GetEntropy` (unary) and `StreamEntropy` (bidi), with
  verbatim `sequence_id` echo (enables qr-sampler `echo_verified`),
  `generation_timestamp_ns`, and `device_id`. qr-sampler clients now work with
  their **default method paths** and can use `bidi_streaming`.
- **Installable package + CLI**: `pip install .` provides `qbert0g` with
  `serve`, `keys` (absorbs `manage_keys.py`), and `check-config` subcommands.
- **UNIX-domain-socket binding** (`server.unix_socket`) alongside TCP.
- **`dragonfly` device type** (qcc serial protocol, high-rate, streaming-mode
  friendly) and **`mock` device type** (os.urandom, loudly labelled NOT
  quantum) for hardware-free development and CI.
- **Freshness contract in config**: `freshness.flush_device_buffer` now
  flushes the serial RX buffer before **every** read (one-shot included, not
  just streaming); `emit_generation_timestamp`; `allow_pooling` /
  `allow_pregeneration` as refuse-if-true declarative guards.
- **Test suite** (46 tests): config, database, devices, both gRPC services
  end-to-end, and a live cross-repo seam test against qr-sampler's
  `QuantumGrpcSource` (unary, bidi, echo-verified prefetch, legacy path).

### Changed

- **Config schema replaced** (strict validation; unknown keys rejected):
  `service:` → `server:` (+ `unix_socket`), `admin_api_key` → `auth.api_key`
  (+ configurable `auth.header`), `database_path` → `database.path`, limit
  defaults → `limits:` (`max_bytes_per_request`, `max_bytes_per_day`,
  `rate_limit_per_minute`), integer `post_processing_mode` → named
  `post_processing.mode: raw|sha256|raw_samples` (global, per-device
  override). Default TCP bind is now `127.0.0.1` (binding `0.0.0.0` warns).
- **Layout**: `app/` + root `proto/` → `src/qbert0g/` (`config`, `database`,
  `devices`, `gate`, `server`, `cli`, `proto/`). Global singletons
  (`get_config`, `get_database`, `get_device_manager`) removed — everything
  is constructed explicitly and injected.
- Both services share one request pipeline (`gate.RequestGate`): auth, rate
  limit, byte caps, failover and usage accounting are protocol-independent.
- Missing config file is now a startup **error** (previously silently ran on
  defaults).

### Removed

- `modal/` (Modal/cloudflared client), `run.py`, `setup.sh`,
  `manage_keys.py` (→ `qbert0g keys`), `example_client.py`
  (→ `examples/client.py`), `requirements.txt` (→ `pyproject.toml`).
