"""``qbert0g`` command-line interface.

Subcommands:

- ``qbert0g serve [--config PATH]`` — run the gRPC server.
- ``qbert0g keys <list|create|update|enable|disable|delete|usage> ...``
  — manage API keys in the server's SQLite store. Keys bind to ANY
  source id: device, PRNG control, or profile.
- ``qbert0g check-config [--config PATH]`` — validate the config file
  and print the resolved shape without starting anything.
- ``qbert0g sources list`` — every source in the namespace (devices,
  controls, profiles) with kind, transform, inputs and availability.
- ``qbert0g sources watch --ids A[,B]`` — live bitstream viewer for
  eyeballing generation-vs-display timing and cross-device synchrony
  of the raw streams feeding an xnor gate. Reads go through the exact
  SourceRouter paired-read serving path.
- ``qbert0g profiles pull --id X --bytes N --out F`` — offline
  generation through the EXACT SourceRouter serving path (no gRPC, no
  gate), for feeding ent / PractRand; writes a provenance record with
  ``protocol: "cli"``.

Config resolution everywhere: ``--config`` > ``QBERT0G_CONFIG`` env >
``./config.yaml``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .config import Config, ConfigError
from .database import Database


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "default"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.0f}TB"


def _fmt_limit(val, suffix: str = "") -> str:
    return "default" if val is None else f"{val}{suffix}"


# ── keys subcommands ─────────────────────────────────────────────────────


async def _cmd_keys_list(db: Database) -> None:
    keys = await db.list_api_keys()
    if not keys:
        print("No API keys found.")
        return
    print(
        f"{'ID':<36}  {'PREFIX':<10}  {'NAME':<20}  {'DEVICE':<12}  "
        f"{'ADMIN':<6}  {'ENABLED':<8}  {'RATE/min':<10}  {'DAILY':<12}  "
        f"{'MAX/REQ':<10}  LAST USED"
    )
    print("-" * 158)
    for k in keys:
        print(
            f"{k['id']:<36}  {k['key_prefix']:<10}  {k['name']:<20}  "
            f"{k['primary_device_id']:<12}  "
            f"{'yes' if k['is_admin'] else 'no':<6}  "
            f"{'yes' if k['enabled'] else 'no':<8}  "
            f"{_fmt_limit(k['rate_limit']):<10}  "
            f"{_fmt_bytes(k['daily_byte_limit']):<12}  "
            f"{_fmt_bytes(k['max_bytes_per_request']):<10}  "
            f"{k['last_used_at'] or 'never'}"
        )


async def _cmd_keys_create(db: Database, args: argparse.Namespace) -> None:
    raw_key, info = await db.create_api_key(
        name=args.name,
        primary_device_id=args.device,
        is_admin=args.admin,
        rate_limit=args.rate_limit,
        daily_byte_limit=args.daily_bytes,
        max_bytes_per_request=args.max_bytes,
    )
    print("=" * 60)
    print("API KEY CREATED — store the key securely.")
    print("It will NOT be shown again.")
    print("=" * 60)
    print(f"  Key:                {raw_key}")
    print(f"  ID:                 {info['id']}")
    print(f"  Name:               {info['name']}")
    print(f"  Device:             {info['primary_device_id']}")
    print(f"  Admin:              {'yes' if info['is_admin'] else 'no'}")
    print(f"  Rate limit:         {_fmt_limit(info['rate_limit'], '/min')}")
    print(f"  Daily limit:        {_fmt_bytes(info['daily_byte_limit'])}")
    print(f"  Max bytes/request:  {_fmt_bytes(info['max_bytes_per_request'])}")
    print(f"  Created:            {info['created_at']}")
    print("=" * 60)


async def _cmd_keys_update(db: Database, args: argparse.Namespace) -> None:
    key = await db.get_api_key_by_id(args.id)
    if not key:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        raise SystemExit(1)
    kwargs = {
        k: v
        for k, v in {
            "name": args.name,
            "primary_device_id": args.device,
            "rate_limit": args.rate_limit,
            "daily_byte_limit": args.daily_bytes,
            "max_bytes_per_request": args.max_bytes,
        }.items()
        if v is not None
    }
    if not kwargs:
        print("Nothing to update — specify at least one option.", file=sys.stderr)
        raise SystemExit(1)
    await db.update_api_key(args.id, **kwargs)
    updated = await db.get_api_key_by_id(args.id)
    print(f"Updated key '{updated['name']}' ({updated['key_prefix']}...):")
    print(f"  Name:               {updated['name']}")
    print(f"  Device:             {updated['primary_device_id']}")
    print(f"  Rate limit:         {_fmt_limit(updated['rate_limit'], '/min')}")
    print(f"  Daily limit:        {_fmt_bytes(updated['daily_byte_limit'])}")
    print(f"  Max bytes/request:  {_fmt_bytes(updated['max_bytes_per_request'])}")


async def _cmd_keys_delete(db: Database, args: argparse.Namespace) -> None:
    key = await db.get_api_key_by_id(args.id)
    if not key:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        raise SystemExit(1)
    if not args.yes:
        confirm = input(f"Delete key '{key['name']}' ({key['key_prefix']}...)? [y/N] ")
        if confirm.lower() != "y":
            print("Aborted.")
            return
    await db.delete_api_key(args.id)
    print(f"Deleted key '{key['name']}'.")


async def _cmd_keys_toggle(db: Database, args: argparse.Namespace, enabled: bool) -> None:
    ok = await db.update_api_key(args.id, enabled=enabled)
    if not ok:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Key {args.id} {'enabled' if enabled else 'disabled'}.")


async def _cmd_keys_usage(db: Database, args: argparse.Namespace) -> None:
    stats = await db.get_usage_stats(args.id, days=args.days)
    if not stats:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Usage for '{stats['key_name']}' (device: {stats['primary_device_id']})")
    print(f"  Period:          last {stats['period_days']} days")
    print(f"  Total requests:  {stats['total_requests']}")
    print(f"  Total bytes:     {_fmt_bytes(stats['total_bytes'])}")
    print(f"  Today requests:  {stats['today_requests']}")
    print(f"  Today bytes:     {_fmt_bytes(stats['today_bytes'])}")
    if stats["daily_byte_limit"]:
        print(f"  Daily limit:     {_fmt_bytes(stats['daily_byte_limit'])}")
    if stats["history"]:
        print()
        print(f"  {'DATE':<12}  {'REQUESTS':>10}  {'BYTES':>12}")
        print(f"  {'-' * 38}")
        for day in stats["history"]:
            print(
                f"  {day['date']:<12}  {day['requests']:>10}  "
                f"{_fmt_bytes(day['bytes_served']):>12}"
            )


async def _run_keys(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    db = Database(config.database_path)
    await db.connect(bootstrap_admin_key=config.auth.api_key)
    try:
        if args.keys_command == "list":
            await _cmd_keys_list(db)
        elif args.keys_command == "create":
            await _cmd_keys_create(db, args)
        elif args.keys_command == "update":
            await _cmd_keys_update(db, args)
        elif args.keys_command == "delete":
            await _cmd_keys_delete(db, args)
        elif args.keys_command == "enable":
            await _cmd_keys_toggle(db, args, enabled=True)
        elif args.keys_command == "disable":
            await _cmd_keys_toggle(db, args, enabled=False)
        elif args.keys_command == "usage":
            await _cmd_keys_usage(db, args)
    finally:
        await db.disconnect()


# ── sources / profiles subcommands ───────────────────────────────────────


async def _with_router(config: Config, fn) -> None:
    """Run *fn(router)* against an initialized DeviceManager + SourceRouter.

    The same construction path the server uses — CLI reads exercise the
    exact serving code, only bypassing gRPC and the request gate.
    """
    from .devices import DeviceManager  # deferred: device stack is heavy
    from .sources import SourceRouter

    devices = DeviceManager(config)
    await devices.initialize()
    try:
        await fn(SourceRouter(config, devices))
    finally:
        await devices.shutdown()


async def _run_sources_list(args: argparse.Namespace) -> None:
    config = Config.load(args.config)

    async def _list(router) -> None:
        rows = router.describe()
        print(f"{'ID':<24}  {'KIND':<8}  {'TRANSFORM':<12}  {'INPUTS':<32}  AVAILABILITY")
        print("-" * 100)
        for row in rows:
            print(
                f"{row['id']:<24}  {row['kind']:<8}  {row['transform']:<12}  "
                f"{row['inputs']:<32}  {row['availability']}"
            )

    await _with_router(config, _list)


def render_watch_sample(
    ids: Sequence[str],
    chunks: Sequence[bytes],
    capture_ns: Sequence[int],
    render_ns: int,
    skew_ns: int | None,
    running_agree_bits: int = 0,
    running_total_bits: int = 0,
) -> tuple[str, int, int]:
    """Render one watch sample → ``(text_block, agree_bits, total_bits)``.

    Pure function (all timestamps passed in) so the format is golden-
    testable. Bit order is the NORMATIVE profile convention (MSB first,
    :func:`qbert0g.profiles.unpack_msb_first`). Layout per sample:
    one line per stream (grouped bits, capture timestamp, capture-to-
    render latency), and for pairs an agreement line between them —
    ``|`` where the bits agree (the XNOR output), ``.`` where they
    differ — with the row and running agreement % and the pair skew in
    microseconds. Deliberately ASCII-only: the viewer must survive
    cp1252 pipes on Windows consoles.
    """
    from .profiles import unpack_msb_first  # deferred: numpy is heavy for --help

    width = max(len(i) for i in ids)
    bit_rows = [unpack_msb_first(chunk) for chunk in chunks]

    def _bits_text(bits) -> str:
        text = "".join(str(b) for b in bits)
        return " ".join(text[i : i + 8] for i in range(0, len(text), 8))

    def _stream_line(idx: int) -> str:
        lat_us = (render_ns - capture_ns[idx]) // 1000
        return (
            f"{ids[idx]:<{width}}  {_bits_text(bit_rows[idx])}  "
            f"cap={capture_ns[idx]}ns  lat={lat_us}us"
        )

    if len(ids) == 1:
        return _stream_line(0), 0, 0

    agree = bit_rows[0] == bit_rows[1]  # XNOR of the two bitstreams
    marks = _bits_text(agree.astype(int))
    marks = marks.replace("1", "|").replace("0", ".")
    row_agree, row_total = int(agree.sum()), int(agree.size)
    running_pct = 100.0 * (running_agree_bits + row_agree) / (running_total_bits + row_total)
    skew_us = (skew_ns or 0) // 1000
    agree_line = (
        f"{'agree':<{width}}  {marks}  "
        f"{100.0 * row_agree / row_total:.1f}% (running {running_pct:.1f}%)  dt={skew_us}us"
    )
    block = "\n".join([_stream_line(0), agree_line, _stream_line(1)])
    return block, row_agree, row_total


async def _run_sources_watch(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    ids = [part.strip() for part in args.ids.split(",") if part.strip()]
    if len(ids) not in (1, 2):
        print("Error: --ids takes one or two comma-separated device ids", file=sys.stderr)
        raise SystemExit(2)

    async def _watch(router) -> None:
        agree_bits = total_bits = 0
        rows = 0
        while args.rows is None or rows < args.rows:
            sample = await router.watch_read(ids, args.bytes_per_row)
            block, row_agree, row_total = render_watch_sample(
                ids,
                sample.chunks,
                sample.capture_ns,
                time.monotonic_ns(),
                sample.skew_ns,
                agree_bits,
                total_bits,
            )
            agree_bits += row_agree
            total_bits += row_total
            print(block, flush=True)
            rows += 1
            if args.interval > 0 and (args.rows is None or rows < args.rows):
                await asyncio.sleep(args.interval)

    await _with_router(config, _watch)


async def _run_profiles_pull(args: argparse.Namespace) -> None:
    config = Config.load(args.config)

    async def _pull(router) -> None:
        read = await router.read(args.id, args.bytes)  # no timeout: operator tool
        Path(args.out).write_bytes(read.data)
        record = router.record_provenance(read, protocol="cli", served_bytes=args.bytes)
        print(f"Wrote {len(read.data)} bytes from {read.source_id!r} to {args.out}")
        print(f"Provenance record {record['request_id']} appended to {config.provenance.path}")

    await _with_router(config, _pull)


# ── top-level commands ───────────────────────────────────────────────────


def _cmd_serve(args: argparse.Namespace) -> None:
    from .server import serve  # deferred: grpc import is heavy

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = Config.load(args.config)
    asyncio.run(serve(config))


def _cmd_check_config(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    print("Config OK.")
    print(f"  listen:           {config.server.listen or '(disabled)'}")
    print(f"  unix_socket:      {config.server.unix_socket or '(disabled)'}")
    print(f"  database:         {config.database_path}")
    print(f"  post_processing:  {config.post_processing_mode}")
    print(f"  flush_on_request: {config.freshness.flush_device_buffer}")
    print(f"  auth header:      {config.auth.header}")
    print(f"  bootstrap admin:  {'yes' if config.auth.api_key else 'no'}")
    strictness = "strict" if config.provenance.strict else "best-effort"
    print(f"  provenance:       {config.provenance.path} ({strictness})")
    print(f"  devices:          {len(config.devices)}")
    for dev in config.devices:
        if dev.type == "chardev":
            # No qcc post-processing chain; DMA output served as-is.
            mode = "n/a (raw DMA)"
            extra = f"pci_address={dev.pci_address}" if dev.pci_address else "no pci_address"
        else:
            mode = dev.post_processing or config.post_processing_mode
            extra = "streaming" if dev.streaming_mode else "one-shot"
        print(
            f"    - {dev.id} ({dev.type}, {dev.path or 'no path'}, "
            f"post_processing={mode}, {extra})"
        )
    print(f"  controls:         {len(config.controls)}")
    for ctl in config.controls:
        detail = f", model={ctl.model}" if ctl.model else ""
        print(f"    - {ctl.id} ({ctl.type}, seeded{detail}) -- PRNG, NOT quantum")
    print(f"  profiles:         {len(config.profiles)}")
    for prof in config.profiles:
        params = ""
        if prof.transform == "parity":
            params = f", taps={list(prof.taps)}, stride={prof.stride}"
        print(f"    - {prof.id} ({prof.transform} over {prof.inputs}{params})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qbert0g",
        description="Qbert0G — quantum entropy gRPC service (QuantumRNG + EntropyService).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run the gRPC server")
    p_serve.add_argument(
        "--config", help="Path to config.yaml (default: $QBERT0G_CONFIG or ./config.yaml)"
    )

    p_check = sub.add_parser("check-config", help="Validate the config file and exit")
    p_check.add_argument("--config", help="Path to config.yaml")

    p_keys = sub.add_parser("keys", help="Manage API keys")
    p_keys.add_argument("--config", help="Path to config.yaml (locates the database)")
    keys_sub = p_keys.add_subparsers(dest="keys_command", required=True)

    keys_sub.add_parser("list", help="List all API keys")

    p_create = keys_sub.add_parser("create", help="Create a new API key")
    p_create.add_argument("--name", required=True, help="Descriptive name for the key")
    p_create.add_argument(
        "--device",
        required=True,
        help="Primary source ID — a device, PRNG control, or profile "
        "(or * for any available device)",
    )
    p_create.add_argument("--admin", action="store_true", help="Grant admin privileges")
    p_create.add_argument("--rate-limit", type=int, metavar="RPM", help="Requests per minute")
    p_create.add_argument("--daily-bytes", type=int, metavar="BYTES", help="Daily byte limit")
    p_create.add_argument("--max-bytes", type=int, metavar="BYTES", help="Max bytes per request")

    p_update = keys_sub.add_parser("update", help="Update settings on an existing key")
    p_update.add_argument("--id", required=True, help="Key ID")
    p_update.add_argument("--name", help="New name")
    p_update.add_argument("--device", help="New primary device ID")
    p_update.add_argument("--rate-limit", type=int, metavar="RPM")
    p_update.add_argument("--daily-bytes", type=int, metavar="BYTES")
    p_update.add_argument("--max-bytes", type=int, metavar="BYTES")

    p_delete = keys_sub.add_parser("delete", help="Delete an API key")
    p_delete.add_argument("--id", required=True, help="Key ID")
    p_delete.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    for name, help_text in (("enable", "Enable a disabled key"), ("disable", "Disable a key")):
        p = keys_sub.add_parser(name, help=help_text)
        p.add_argument("--id", required=True, help="Key ID")

    p_usage = keys_sub.add_parser("usage", help="Show usage stats for a key")
    p_usage.add_argument("--id", required=True, help="Key ID")
    p_usage.add_argument("--days", type=int, default=7, help="History window in days (default: 7)")

    p_sources = sub.add_parser("sources", help="Inspect the source namespace")
    p_sources.add_argument("--config", help="Path to config.yaml")
    sources_sub = p_sources.add_subparsers(dest="sources_command", required=True)
    sources_sub.add_parser(
        "list", help="List all devices, controls and profiles with availability"
    )
    p_watch = sources_sub.add_parser(
        "watch",
        help="Live side-by-side bitstream viewer for one or two raw device streams",
    )
    p_watch.add_argument(
        "--ids",
        required=True,
        help="One or two comma-separated device ids (e.g. dragonfly-0,dragonfly-1)",
    )
    p_watch.add_argument(
        "--bytes-per-row", type=int, default=4, help="Bytes read per row (default: 4)"
    )
    p_watch.add_argument(
        "--rows", type=int, default=None, help="Stop after N rows (default: until Ctrl-C)"
    )
    p_watch.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Seconds between rows (default: 0.5; 0 = as fast as the devices allow)",
    )

    p_profiles = sub.add_parser("profiles", help="Offline profile operations")
    p_profiles.add_argument("--config", help="Path to config.yaml")
    profiles_sub = p_profiles.add_subparsers(dest="profiles_command", required=True)
    p_pull = profiles_sub.add_parser(
        "pull",
        help="Generate bytes through the exact serving path (no gRPC) and write them to a file",
    )
    p_pull.add_argument("--id", required=True, help="Source ID (profile, control, or device)")
    p_pull.add_argument("--bytes", required=True, type=int, help="Number of bytes to generate")
    p_pull.add_argument("--out", required=True, help="Output file path")

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            _cmd_serve(args)
        elif args.command == "check-config":
            _cmd_check_config(args)
        elif args.command == "keys":
            asyncio.run(_run_keys(args))
        elif args.command == "sources":
            if args.sources_command == "watch":
                asyncio.run(_run_sources_watch(args))
            else:
                asyncio.run(_run_sources_list(args))
        elif args.command == "profiles":
            asyncio.run(_run_profiles_pull(args))
    except KeyboardInterrupt:
        pass  # Ctrl-C on `sources watch` (and friends) exits cleanly
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
