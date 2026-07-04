# Qbert0G — agent guide

gRPC daemon serving freshly measured quantum noise. Three services, one
process, one shared request pipeline. No framework magic.

## Verification oracles (run these; treat output as ground truth)

```bash
python -m ruff check .
python -m pytest tests/ -q        # no hardware needed (mock device + tmp fixtures)
```

Or `make check`. A change is not done until both pass.

## Layering (imports point downward only)

```
cli.py  ──►  server.py  ──►  gate.py  ──►  sources.py (SourceRouter)
                 │               │             ├──► profiles.py  (transforms)
                 ├──► proto/     │             ├──► controls.py  (PRNG sources)
                 │               │             └──► devices.py
                 │               └──► database.py
                 ├──► coherence.py ──► sources.py   (CoherenceMonitor)
                 ├──► integrators.py ──► fingerprint.py
                 └──► purity.py      ──► fingerprint.py
all of the above ──► config.py   (config.py imports nothing internal;
                                  profiles.py/controls.py/fingerprint.py
                                  import ONLY config.py)
```

The QPI modules (`coherence.py` / `integrators.py` / `purity.py` /
`fingerprint.py`) are drawn under `server.py`, but `cli.py` also imports
them directly for the offline commands (`draws pull`, `coherence null`,
`sources describe`) — those edges still point strictly downward.

- `config.py` — strict schema; **unknown keys are a startup error**. All
  tunables live here; no other module reads YAML or env vars.
- `database.py` — API keys (SHA-256 hashed) + usage in SQLite. No singleton:
  `Database(path)`, `connect(bootstrap_admin_key=...)`.
- `devices.py` — `DeviceManager(config)`: pyqcc drivers (firefly / qcicada /
  dragonfly), `ChardevDevice` (PCIe Dragonfly at `/dev/qrngDF*`; DMA reads,
  no pyqcc/post-processing; sysfs `ready_count` drain + `error_present`/
  `error_bits` health snapshot when `pci_address` is set) + `MockDevice`
  (os.urandom, dev only). Per-device asyncio locks, failover routing,
  freshness flush before EVERY read (config-gated) — `_flush_input` is the
  single pre-measurement seam for all device types. The SourceRouter seam
  (`acquire_for_profile` + `read_chunk_locked`) holds ONE device with a
  single request-start flush for chunked paired reads.
- `controls.py` — seeded PRNG sources (`prng_uniform`, `prng_markov`),
  loudly NOT quantum; every served block regenerable offline from
  `(id, seed, stream_offset_bytes)`.
- `profiles.py` — pure NORMATIVE transforms (`identity`, `xnor`, `parity`;
  bit order = numpy unpackbits/packbits, MSB first) + the `Profile`
  descriptor. No I/O.
- `sources.py` — `SourceRouter`: ONE id namespace over devices + controls +
  profiles; paired-read choreography (lexicographic lock order, alternating
  chunks, skew measurement); `ProvenanceLog` (append-only JSONL, one record
  per served request); `watch_read` (raw device samples for the CLI
  `sources watch` bitstream viewer, through the same paired-read path).
- `fingerprint.py` — frozen per-source statistical baselines (QPI):
  `load_fingerprint(path) -> (Fingerprint, sha256)`, loud validation incl. a
  neff-vs-ACF cross-check. Fitted offline by `scripts/fit_fingerprint.py`.
- `integrators.py` — pure integration statistics `integrate(name, raw, fp)
  -> IntegrationResult(z, u, aux)`; the name sets (`INTEGRATOR_TYPES` /
  `SERVE_INTEGRATORS` / `AUX_INTEGRATORS`) live in `config.py`.
- `purity.py` — the purity taxonomy (`Origin`/`Integrity`/`Processing`),
  `EntropyLabel` with canonical round-trip, static label derivation.
- `coherence.py` — pure `block_correlation` (lag-scanned Pearson r, Fisher
  z_c) + `CoherenceMonitor` (background device-pair evaluation loop; reads
  via `SourceRouter.coherence_pair_read`).
- `gate.py` — `RequestGate.measure(context, n, protocol=..., sequence_id=...,
  requested_source_id=..., provenance_extras=...)`:
  auth → rate limit → byte caps → source read → provenance → usage record.
  **Every RPC of all three services goes through this one method**; never add
  a second validation path. API keys bind to ANY source id.
- `server.py` — the three servicers + `QpiContext` composition +
  `QbertServer` (testable start/stop) + `serve()` (signals). Binds TCP
  and/or UDS.
- `proto/` — `qrng.proto` (public), `entropy_service.proto` and
  `purity_service.proto` (byte-identical copies of qr-sampler's protos).
  Generated stubs are committed; regenerate with `make proto` (its file list
  must name all three .proto files; regeneration must be a no-op diff).

## The three protocols (do not break these invariants)

| | `qrng.QuantumRNG` | `qr_entropy.EntropyService` | `qr_purity.PurityService` |
|---|---|---|---|
| consumers | general public clients | qr-sampler / qthought | qr-sampler server-draw mode |
| request | `num_bytes` (field 1) | `bytes_needed` (1), `sequence_id` (2) | `sequence_id` (1), `source_id` (2), `block_bytes` (3) |
| response | `data` (1), `timestamp` µs (2), `device_id` (3) | `data` (1), `sequence_id` echo (2), `generation_timestamp_ns` (3), `device_id` (4) | `u` (1), `z` (2), `sequence_id` echo (3), `generation_timestamp_ns` (4), `source_id` (5), `coherence_z` (6), `coherence_valid` (7), `purity_label` (8), `integrated_bytes` (9), `integrator` (10), `coherence_r` (11) |
| RPCs | `GetRandomBytes` | `GetEntropy` + `StreamEntropy` (bidi) | `GetDraw` + `StreamDraws` (bidi) |

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
6. **Profiles never fail over.** An experiment arm's composition is fixed:
   any unavailable input fails the request `UNAVAILABLE`. Plain device ids
   keep classic failover. Never "helpfully" substitute an input.
7. **Provenance never fails a request** (write failure → ERROR log, request
   served) — unless `provenance.strict: true`, which inverts this for study
   runs. Per-input `kind` (`"quantum"` / `"prng"` / `"mock"`) must always be
   recorded; pseudorandom involvement is never laundered.
8. **No entropy/uniformity threshold is ever a pass criterion on served
   output** — matched-distribution PRNG controls are legitimately
   non-uniform. The pipeline proof is provenance replay: regenerate a served
   block offline from its record alone and assert byte identity. Statistical
   assertions live ONLY in transform unit tests against constructed inputs
   (this explicitly includes the QPI integrator/coherence tests: KS-uniformity
   and null pins run on synthetic/mock data at the transform level).
9. **Fingerprints are frozen at load.** No runtime baseline adaptation — a
   drifting baseline absorbs exactly the sustained signal the integrators
   exist to see. `load_fingerprint` refuses silently edited files (neff-vs-ACF
   cross-check); re-characterization is an operator action between runs.
10. **`coherence_valid` is never faked.** Monitor absent, disabled, stale
    (`max_age_s`), or never computed ⇒ `coherence_valid=false` on the draw —
    never a fabricated zero. A failed evaluation leaves the last value as-is;
    staleness is what downgrades it.
11. **The serve path refuses non-serve integrators.** Only `SERVE_INTEGRATORS`
    (`bit_z`, `byte_z`) may produce a served `u`; `default_integrator` outside
    that set is a startup `ConfigError`. Aux (`cusum`, `rw_excursion`) are
    provenance-only secondaries; `majority_vote`/`kmer_mode` are offline/CLI
    statistics. Nobody samples tokens off a mode statistic.
12. **One validation path includes the PurityService.** Every draw goes
    through `RequestGate.measure` (`protocol="qr_purity"`, `block_bytes`
    accounted) and produces exactly one provenance record, carrying the draw
    extras (`integrator`, `z`, `purity_label`, `fingerprint_sha256`,
    `coherence`). `requested_source_id` is honored only for draws and only
    for ids in `integration.sources`. Success responses never carry `u`
    outside `(1e-10, 1 - 1e-10)`; `sequence_id` is echoed verbatim.

## How to extend

- **New device type**: add to `DEVICE_TYPES` (+ `QCC_DEVICE_TYPES` and
  `ONE_SHOT_LIMITS` if qcc-driven; types without a limit are unbounded),
  branch in `DeviceManager._connect_device` if it needs a different driver.
  Mock-style drivers just need `start_one_shot / start_continuous /
  read_continuous / stop / close_comm` (see `ChardevDevice` for a minimal
  non-serial example). Pre-measurement behavior (freshness/health) goes in
  `_flush_input`, never in a second seam.
- **New config key**: add the field to the dataclass in `config.py`, the key
  to the `_check_keys` allowlist, a default, and a test in `tests/test_config.py`.
- **New RPC/service**: implement the servicer in `server.py`, route every
  request through `RequestGate.measure`, register it in `QbertServer.start`.
- **New transform**: pure function in `profiles.py` (pin its bit order in a
  comment AND a test) + arity entry in `config.py:TRANSFORM_ARITY` + a branch
  in `Profile.apply`/`raw_bytes_needed` + param validation in
  `config._parse_profiles` if it takes params + golden-vector tests. The
  SourceRouter needs no changes unless the read choreography differs.
- **New control type**: class in `controls.py` exposing `read(n)`,
  `stream_offset_bytes`, `kind = "prng"` and a `config` attr; register in
  `make_control` + `config.py:CONTROL_TYPES`. Keep it seeded and offline-
  regenerable — record whatever facts regeneration needs in the provenance
  fact (see `SourceRouter._control_bytes`).
- **New integrator**: pure function `(raw: bytes, fp: Fingerprint) ->
  IntegrationResult` in `integrators.py` + an entry in its `_INTEGRATORS`
  dispatch dict + the name added to `config.py:INTEGRATOR_TYPES` (the module
  asserts the two stay in sync). Then decide its class deliberately:
  `SERVE_INTEGRATORS` only if its `u` is honestly uniform under the null
  (fingerprint-referenced, Φ-mapped, clamped); `AUX_INTEGRATORS` if it is a
  provenance-only secondary; neither for offline/CLI statistics. Reference
  every parameter to the fingerprint, never to ideal values; correct standard
  errors with `fp.neff_factor`. Add golden hand-computed vectors to
  `tests/test_integrators.py`.

## Cross-repo context

- Sibling repos: `qr-sampler` (the sampling library whose
  `QuantumGrpcSource` is this server's primary client) and `qr-llm-qthought`
  (the cognition service; its broker draws through qr-sampler). Their
  deployment docs live in `qr-llm-qthought/docs/` (`SETUP_GUIDE.md`,
  `SECRETS_MAP.md`) and its `infra/qthought-qbert0g.service` runs this daemon
  as `qbert0g serve --config /etc/qthought/qbert0g.config.yaml`.
- `entropy_service.proto` and `purity_service.proto` are byte-identical
  copies of qr-sampler's protos (`entropy_service.proto` is deliberately
  frozen). Byte-identity of `purity_service.proto` is enforced by the SAME
  pinned sha256 constant in both repos' tests
  (`tests/test_purity_service.py::TestProtoPin` here). If a proto changes,
  update BOTH repos + BOTH pins in one increment, and run `make proto` —
  the Makefile `proto` target's file list must name all three .proto files,
  and regeneration must produce a no-op diff on committed stubs.
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
