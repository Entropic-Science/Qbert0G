# qr-server deployment — the shared entropy daemon config

This directory holds the Qbert0G side of the **box-level "self-containment"
qr-server profile**: ONE `qbert0g` daemon owning both Dragonfly cards, serving
every on-box client (the shared qr-sampler vLLM, qthought's broker, owui, any
future external caller) on `/run/qbert0g/qbert0g.sock`.

**CANONICAL PAIRING RULE:** `qbert0g.config.yaml.example` here is kept
**byte-identical** to
`qr-sampler/deployments/qr-server/qbert0g.config.yaml.example` (the qr-server
profile consumes it). Edit both together and verify with a file diff. The full
profile (systemd units, vLLM env, install steps, topology) lives in the
qr-sampler copy's `README.md`; this file covers only what the daemon operator
needs.

## Provisioning API keys

Created with `qbert0g keys create`; keys bind to any source id (device,
control, or profile). The draw keys bind to `dragonfly-0`; the PRNG study keys
bind to the seeded control sources:

```bash
# Draw keys — all bound to the sole draw card:
qbert0g keys create --name qr-sampler --device dragonfly-0 --max-bytes 2097152
qbert0g keys create --name qthought   --device dragonfly-0
qbert0g keys create --name external   --device dragonfly-0 --max-bytes 2097152

# PRNG-vs-QRNG study lanes (qr-llm-research) — bound to the `controls:`
# entries in qbert0g.config.yaml. Draws through these keys produce
# `kind: "prng"` provenance records — loudly pseudorandom, never laundered.
qbert0g keys create --name qr-sampler-prng-uniform --device prng_uniform
qbert0g keys create --name qr-sampler-prng-markov  --device prng_markov
qbert0g keys create --name qr-sampler-prng-resample --device prng_resample
```

The study keys are pasted into the shared vLLM's
`QR_ENTROPY_SOURCE_INSTANCES` (`qbert_prng_uniform` / `qbert_prng_markov` /
`qbert_prng_resample` instances in `qr-server.env` — see the qr-sampler
profile README).

**No key binds draws to `dragonfly-1`** — it is the coherence reference only.

## `prng_markov` prerequisite — fit the model first (operator step)

The `prng_markov` control loads an order-1 byte Markov model (npz) at daemon
**startup** — config parsing alone does not require the file, starting the
server does. Fit it from raw dragonfly-0 dumps before enabling the control:

```bash
python scripts/fit_markov.py --device-id dragonfly-0 \
    --out /etc/qthought/models/dragonfly-0_markov_v1.npz dump1.bin [dump2.bin ...]
```

The script prints a model-vs-dumps fingerprint summary (byte mean, per-bit
P(1), lag-1 byte correlation) side by side — eyeball the fit before use. The
model must validate (row-stochastic within 1e-9) or the daemon refuses to
start. Until the npz exists, comment out the `prng_markov` control (and skip
its key).

## `prng_resample` prerequisite — record the pool first (operator step)

The `prng_resample` control serves seeded-PRNG-chosen contiguous windows of a
frozen raw dump of dragonfly-0. Record it **through the running daemon** (it
must stay the only reader of the device nodes), with a key bound to
dragonfly-0:

```bash
QBERT0G_API_KEY=<a dragonfly-0 key> python scripts/record_pool.py \
    --device-id dragonfly-0 --bytes 268435456 \
    --out /var/lib/qbert0g/pools/dragonfly-0_pool_v1.bin
```

The script prints the pool's sha256; the daemon recomputes it at startup and
writes it into every provenance fact of the control. Pools are frozen: the
script refuses to overwrite one. The pool does not follow later device drift,
so re-record under a new name (and update `pool:`) between study runs. Until
the pool exists, comment out the `prng_resample` control (and skip its key).

## Per-request conditioning (`hash_truncate`)

With `conditioning.allow_request: true` a byte-path caller may ask for hashed,
truncated bytes instead of raw ones by sending `qr-conditioning:
hash_truncate` (plus optional `qr-conditioning-hash`, `-in-bytes`,
`-keep-bytes`) in its gRPC metadata. qr-sampler does this from its
`qr_conditioning_*` request keys. Nothing changes for callers that send no
header. To see what a raw dump looks like to a z-score sampler under each
mode before choosing parameters:

```bash
python scripts/conditioning_check.py dump.bin --resample-seed 0x13198a2e03707344a4093822299f31d0
```

## `provenance.strict: true` — recommended for study runs

For PRNG-vs-QRNG study sessions set `provenance.strict: true` in the deployed
config: a provenance write failure then **fails the draw** instead of
logging-and-serving, so a study arm can never contain unattributable bytes.
The example ships `strict: false` (availability over auditability for
day-to-day serving).
