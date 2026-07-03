# Qbert0G — agent guide

gRPC daemon serving freshly measured quantum noise. Two services, one process,
one shared request pipeline. ~2k LOC, no framework magic.

## Verification oracles (run these; treat output as ground truth)

```bash
python -m ruff check .
python -m pytest tests/ -q        # 46 tests, no hardware needed (mock device)
```

Or `make check`. A change is not done until both pass.

## Layering (imports point downward only)

```
cli.py  ──►  server.py  ──►  gate.py  ──►  devices.py
                 │               │
                 └──► proto/     └──► database.py
all of the above ──► config.py   (config.py imports nothing internal)
```

- `config.py` — strict schema; **unknown keys are a startup error**. All
  tunables live here; no other module reads YAML or env vars.
- `database.py` — API keys (SHA-256 hashed) + usage in SQLite. No singleton:
  `Database(path)`, `connect(bootstrap_admin_key=...)`.
- `devices.py` — `DeviceManager(config)`: pyqcc drivers (firefly / qcicada /
  dragonfly) + `MockDevice` (os.urandom, dev only). Per-device asyncio locks,
  failover routing, freshness flush before EVERY read (config-gated).
- `gate.py` — `RequestGate.measure(context, n)`: auth → rate limit → byte
  caps → device read → usage record. **Every RPC of both services goes
  through this one method**; never add a second validation path.
- `server.py` — the two servicers + `QbertServer` (testable start/stop) +
  `serve()` (signals). Binds TCP and/or UDS.
- `proto/` — `qrng.proto` (public) + `entropy_service.proto` (byte-identical
  copy of qr-sampler's proto). Generated stubs are committed; regenerate with
  `make proto`.

## The two protocols (do not break these invariants)

| | `qrng.QuantumRNG` | `qr_entropy.EntropyService` |
|---|---|---|
| consumers | general public clients | qr-sampler / qthought |
| request | `num_bytes` (field 1) | `bytes_needed` (1), `sequence_id` (2) |
| response | `data` (1), `timestamp` µs (2), `device_id` (3) | `data` (1), `sequence_id` echo (2), `generation_timestamp_ns` (3), `device_id` (4) |
| RPCs | `GetRandomBytes` | `GetEntropy` + `StreamEntropy` (bidi) |

Load-bearing details:

1. **`sequence_id` must be echoed verbatim.** qr-sampler's pipelined prefetch
   sends a 63-bit commitment nonce and sets `echo_verified` only on an exact
   echo. Pinned by `tests/test_qr_sampler_seam.py::test_pipelined_prefetch_is_echo_verified`.
2. **Response field numbers are frozen.** The two protos collide on response
   fields 2/3 (timestamp-µs vs sequence-id echo); qr-sampler documents this
   collision on its decode site and tolerates the legacy path *because* the
   field numbers are what they are. Renumbering either proto breaks live
   clients silently.
3. **Never send an empty `data` payload on success** — qr-sampler's decoder
   treats it as `EntropyUnavailableError`.
4. **`post_processing.mode: raw` and the pre-read buffer flush are the
   research contract** for qthought (raw noise, freshly measured, never
   pooled). Config guards (`allow_pooling: false`, unknown-key rejection)
   keep this auditable; don't weaken them.
5. **Entropy locality is deployment policy, not code**: binding `0.0.0.0` is
   allowed but warned. qthought deployments bind loopback/UDS only.

## How to extend

- **New device type**: add to `DEVICE_TYPES` (+ `ONE_SHOT_LIMITS` if qcc),
  branch in `DeviceManager._connect_device` if it needs a different driver.
  Mock-style drivers just need `start_one_shot / start_continuous /
  read_continuous / stop / close_comm`.
- **New config key**: add the field to the dataclass in `config.py`, the key
  to the `_check_keys` allowlist, a default, and a test in `tests/test_config.py`.
- **New RPC/service**: implement the servicer in `server.py`, route every
  request through `RequestGate.measure`, register it in `QbertServer.start`.

## Cross-repo context

- Sibling repos: `qr-sampler` (the sampling library whose
  `QuantumGrpcSource` is this server's primary client) and `qr-llm-qthought`
  (the cognition service; its broker draws through qr-sampler). Their
  deployment docs live in `qr-llm-qthought/docs/` (`SETUP_GUIDE.md`,
  `SECRETS_MAP.md`) and its `infra/qthought-qbert0g.service` runs this daemon
  as `qbert0g serve --config /etc/qthought/qbert0g.config.yaml`.
- `entropy_service.proto` is a copy of
  `qr-sampler/src/qr_sampler/proto/entropy_service.proto`. If it ever changes
  upstream (it is deliberately frozen), copy it here and `make proto`.
- The seam tests skip without qr-sampler; install it editable
  (`pip install -e ../qr-sampler`) to run the full contract.

## Gotchas

- Generated `*_pb2*.py` files are committed and excluded from ruff; never
  hand-edit them.
- The server tests run the server on the pytest event loop — use `grpc.aio`
  clients in async tests; blocking sync clients deadlock. The seam test runs
  qr-sampler's sync source via `run_in_executor` for the same reason.
- UDS binding is skipped (with a warning) on Windows.
- `qbert0g keys` and `serve` share the database file; the CLI resolves it
  from the same config file as the server.
