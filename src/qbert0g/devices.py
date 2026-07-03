"""Device management for QRNG hardware.

Drives Crypta Labs devices through the official pyqcc library:

- ``firefly``   (PCIe, ``/dev/ttyACM*``) — one-shot limit 35,200 bytes
- ``qcicada``   (USB,  ``/dev/ttyUSB*``) — one-shot limit 13,440 bytes
- ``dragonfly`` (serial, ``/dev/ttyQRNG*``) — qcc protocol, high-rate
  (~350 Mbit/s); one-shot limit as firefly, best used in streaming mode
- ``chardev``   (PCIe Dragonfly, ``/dev/qrngDF*``) — plain character
  device, DMA reads, no pyqcc and no qcc-cli post-processing chain:
  the server serves whatever the driver DMA delivers. With
  ``pci_address`` configured, freshness flush drains the card FIFO
  (sysfs ``ready_count``) and a health snapshot (``error_present`` /
  ``error_bits``) is taken before every measurement.
- ``mock``      — ``os.urandom``, NO hardware, NOT quantum; for
  development and tests only (logged loudly at startup)

Two read modes per device:

- One-shot (default): ``start_one_shot()`` per request up to the device
  limit; larger requests use a temporary continuous-mode burst.
- Streaming: the device stays in continuous mode; reads call
  ``read_continuous()`` with no size limit; an optional idle timeout
  sleeps the device and the next request wakes it.

Freshness: when ``freshness.flush_device_buffer`` is on (the default),
the serial receive buffer is flushed immediately before EVERY
measurement — one-shot and streaming alike — so no byte measured before
the request is ever served. Post-processing mode is set per device at
startup via ``/opt/firefly/qcc-cli -P <mode>``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

try:
    # pyqcc.__init__ does not expose cmdctrl at the top level.
    from pyqcc import cmdctrl

    PYQCC_AVAILABLE = True
except ImportError:
    PYQCC_AVAILABLE = False

import contextlib

from .config import ONE_SHOT_LIMITS, QCC_DEVICE_TYPES, Config, DeviceConfig

logger = logging.getLogger(__name__)

QCC_CLI = "/opt/firefly/qcc-cli"

#: sysfs directory holding per-card attribute files (``ready_count``,
#: ``error_present``, ``error_bits``). Module-level so tests can point
#: it at a tmp_path fixture — no hardware in CI.
_SYSFS_PCI_ROOT = "/sys/bus/pci/devices"

#: One-shot ceiling for devices without a hardware limit (mock, chardev).
_UNBOUNDED_ONESHOT_LIMIT = 1 << 30


class MockDevice:
    """Software stand-in exposing the pyqcc driver surface.

    Serves ``os.urandom`` — cryptographically strong but NOT quantum and
    NOT freshly measured. Exists so the full server path (auth, limits,
    locking, failover, both gRPC services) can run without hardware.
    """

    def __init__(self) -> None:
        self._continuous = False

    def start_one_shot(self, num_bytes: int) -> bytes:
        return os.urandom(num_bytes)

    def start_continuous(self) -> bool:
        self._continuous = True
        return True

    def read_continuous(self, num_bytes: int) -> bytes:
        return os.urandom(num_bytes)

    def stop(self) -> None:
        self._continuous = False

    def close_comm(self) -> None:
        self._continuous = False


class ChardevDevice:
    """PCIe Dragonfly exposed as a plain character device (``/dev/qrngDF*``).

    Exposes the same driver surface as :class:`MockDevice` so the
    manager's routing/locking/failover paths need no special cases.
    Reads are blocking DMA transfers: one ``os.read`` where possible,
    looping only on short reads. There is no qcc-cli ``-P`` chain and no
    pyqcc — post-processing is not applicable; the server serves
    whatever the driver DMA delivers.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        # O_BINARY is a no-op outside Windows; on Windows (tests only)
        # it prevents newline translation corrupting byte counts.
        self._fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))

    def _read_exact(self, num_bytes: int) -> bytes:
        chunks: list[bytes] = []
        remaining = num_bytes
        while remaining:
            chunk = os.read(self._fd, remaining)
            if not chunk:
                raise RuntimeError(
                    f"Char device {self._path} returned EOF after "
                    f"{num_bytes - remaining}/{num_bytes} bytes"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def drain(self, num_bytes: int) -> int:
        """Read and discard up to *num_bytes*; returns bytes drained.

        Freshness flush: discards everything the card generated before
        this request. EOF-tolerant — a race with the FIFO refilling can
        only leave FEWER stale bytes than ``ready_count`` reported.
        """
        remaining = num_bytes
        while remaining:
            chunk = os.read(self._fd, remaining)
            if not chunk:
                break
            remaining -= len(chunk)
        return num_bytes - remaining

    # ── pyqcc-style driver surface ─────────────────────────────────────

    def start_one_shot(self, num_bytes: int) -> bytes:
        return self._read_exact(num_bytes)

    def start_continuous(self) -> bool:
        return True  # the card streams continuously by nature

    def read_continuous(self, num_bytes: int) -> bytes:
        return self._read_exact(num_bytes)

    def stop(self) -> None:
        pass  # nothing to stop; DMA reads are on-demand

    def close_comm(self) -> None:
        os.close(self._fd)


class DeviceStatus(Enum):
    OFFLINE = "offline"
    ONLINE = "online"
    BUSY = "busy"
    ERROR = "error"


@dataclass
class DeviceState:
    """Runtime state for one device."""

    config: DeviceConfig
    status: DeviceStatus = DeviceStatus.OFFLINE
    driver: Any | None = None  # pyqcc device handle or MockDevice
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    bytes_served: int = 0
    requests_served: int = 0
    last_request_time: float | None = None
    error_message: str | None = None
    streaming_active: bool = False
    # chardev pre-measurement snapshot (latest measurement; provenance in S10):
    last_flushed_bytes: int | None = None  # freshness drain size, ready_count * 4
    last_error_present: str | None = None  # sysfs error_present (raw text)
    last_error_bits: str | None = None  # sysfs error_bits (raw text; clear-on-read)
    flush_warning_emitted: bool = False  # one-time "no pci_address" warning latch

    @property
    def is_available(self) -> bool:
        return self.status == DeviceStatus.ONLINE and not self.lock.locked()

    @property
    def oneshot_limit(self) -> int:
        return ONE_SHOT_LIMITS.get(self.config.type, _UNBOUNDED_ONESHOT_LIMIT)


class DeviceManager:
    """Owns device connections, locking, failover and read routing.

    Each device has a mutex so bytes are never served to two requests
    simultaneously. Requests route primary -> same type -> other types
    (when ``server.failover_enabled``).
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._flush_enabled = config.freshness.flush_device_buffer
        self.devices: dict[str, DeviceState] = {}
        self._idle_monitor_task: asyncio.Task | None = None
        # Dedicated executor for device I/O — separate from the loop's
        # default executor so shutdown never waits on a stuck device thread.
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qbert0g-device")

    # ── lifecycle ──────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Connect all enabled devices; start the idle monitor if needed."""
        for dev_config in self._config.devices:
            if not dev_config.enabled:
                logger.info("Device %s is disabled, skipping", dev_config.id)
                continue
            if dev_config.type in QCC_DEVICE_TYPES and not PYQCC_AVAILABLE:
                logger.error(
                    "pyqcc not available — cannot drive %s (%s). "
                    "Install the pyqcc wheel from Crypta Labs.",
                    dev_config.id,
                    dev_config.type,
                )
                continue
            if dev_config.type == "mock":
                logger.warning(
                    "Device %s is a MOCK (os.urandom) — NOT quantum, NOT freshly "
                    "measured. Development/testing only.",
                    dev_config.id,
                )

            state = DeviceState(config=dev_config)
            self.devices[dev_config.id] = state
            try:
                await self._connect_device(dev_config.id)
                if dev_config.streaming_mode:
                    await self._start_streaming(dev_config.id)
            except Exception as exc:
                state.status = DeviceStatus.ERROR
                state.error_message = str(exc)
                logger.error("Failed to initialize device %s: %s", dev_config.id, exc)

        needs_monitor = any(
            s.config.streaming_mode and s.config.streaming_idle_timeout > 0
            for s in self.devices.values()
        )
        if needs_monitor:
            self._idle_monitor_task = asyncio.create_task(self._idle_monitor())
            logger.info("Streaming idle monitor started")

    async def shutdown(self) -> None:
        """Stop streaming, close connections, abandon the executor."""
        if self._idle_monitor_task:
            self._idle_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_monitor_task
            self._idle_monitor_task = None

        loop = asyncio.get_running_loop()
        for device_id, state in self.devices.items():
            try:
                if state.driver is not None:
                    driver = state.driver

                    def _close(drv: Any = driver, st: DeviceState = state) -> None:
                        if st.streaming_active:
                            with contextlib.suppress(Exception):
                                drv.stop()
                        st.streaming_active = False
                        try:
                            drv.close_comm()
                        except Exception as exc:
                            logger.warning("Error closing device %s: %s", st.config.id, exc)

                    await loop.run_in_executor(self._executor, _close)
                state.status = DeviceStatus.OFFLINE
                logger.info("Device %s shut down", device_id)
            except Exception as exc:
                logger.error("Error shutting down device %s: %s", device_id, exc)

        # Abandon without waiting — in-flight device threads finish or die
        # with the process; cancel_futures drops queued-but-unstarted tasks.
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── connection ─────────────────────────────────────────────────────

    def _flush_input(self, state: DeviceState) -> None:
        """The single pre-measurement freshness seam.

        Serial (pyqcc) devices: clear the RX buffer so no pre-request
        byte is served. pyqcc's ``comm.flush()`` only flushes the write
        side; we reach the underlying pyserial object directly
        (best-effort across pyqcc versions). Chardev devices: drain the
        card FIFO and take a health snapshot (:meth:`_chardev_pre_measurement`).
        No-op for mock devices. The drain/flush half is gated on
        ``freshness.flush_device_buffer``.
        """
        if isinstance(state.driver, ChardevDevice):
            self._chardev_pre_measurement(state)
            return
        if not self._flush_enabled or isinstance(state.driver, MockDevice):
            return
        with contextlib.suppress(Exception):
            state.driver._comm._ser.reset_input_buffer()

    def _chardev_pre_measurement(self, state: DeviceState) -> None:
        """Freshness translation + health snapshot for one chardev read.

        With ``pci_address`` set: read sysfs ``ready_count`` (32-bit
        words) and drain ``ready_count * 4`` bytes from the device node
        so no byte generated before this request is served; then read
        ``error_present`` / ``error_bits`` once. ``error_bits`` is
        clear-on-read — Qbert0G must be the ONLY reader on the box.
        Failures raise: serving possibly-stale bytes silently would
        break the freshness contract (fail loud, let failover route).
        """
        pci_address = state.config.pci_address
        if pci_address is None:
            if self._flush_enabled and not state.flush_warning_emitted:
                state.flush_warning_emitted = True
                logger.warning(
                    "Device %s: chardev without `pci_address` — freshness flush "
                    "is unavailable; served bytes may predate the request",
                    state.config.id,
                )
            return

        sysfs_dir = Path(_SYSFS_PCI_ROOT) / pci_address
        if self._flush_enabled:
            ready_words = int((sysfs_dir / "ready_count").read_text().strip())
            state.last_flushed_bytes = state.driver.drain(ready_words * 4)
        # Health snapshot, once per measurement (S10 attaches it to provenance).
        state.last_error_present = (sysfs_dir / "error_present").read_text().strip()
        state.last_error_bits = (sysfs_dir / "error_bits").read_text().strip()

    async def _connect_device(self, device_id: str) -> None:
        """(Re)connect one device: set post-processing mode, open the port."""
        state = self.devices[device_id]
        config = state.config
        loop = asyncio.get_running_loop()

        if state.driver is not None:
            old = state.driver

            def _close_existing(drv: Any = old) -> None:
                with contextlib.suppress(Exception):
                    drv.close_comm()

            await loop.run_in_executor(self._executor, _close_existing)

        if config.type == "mock":
            state.driver = MockDevice()
            state.status = DeviceStatus.ONLINE
            state.error_message = None
            logger.info("Device %s (mock) ready", device_id)
            return

        if config.type == "chardev":
            # No qcc-cli -P chain, no pyqcc: the char device serves whatever
            # the driver DMA delivers (post_processing is not applicable).
            def _open_chardev() -> ChardevDevice:
                return ChardevDevice(config.path)

            try:
                state.driver = await loop.run_in_executor(self._executor, _open_chardev)
            except Exception as exc:
                state.status = DeviceStatus.ERROR
                state.error_message = str(exc)
                raise RuntimeError(f"Failed to initialize device {device_id}: {exc}") from exc
            state.status = DeviceStatus.ONLINE
            state.error_message = None
            logger.info("Device %s (chardev %s) ready", device_id, config.path)
            return

        qcc_mode = self._config.qcc_mode_for(config)

        def _create_device() -> Any:
            # Set post-processing via qcc-cli before opening the port; the
            # CLI exits immediately so there is no port conflict.
            result = subprocess.run(
                [QCC_CLI, "-d", config.path, "-P", str(qcc_mode)],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"qcc-cli -P {qcc_mode} failed for {device_id}: "
                    f"{result.stderr.decode().strip()}"
                )
            logger.info("Device %s post-processing mode set to %d", device_id, qcc_mode)
            return cmdctrl.device("serial", config.path)

        try:
            state.driver = await loop.run_in_executor(self._executor, _create_device)
            state.status = DeviceStatus.ONLINE
            state.error_message = None
            logger.info("Device %s connected successfully", device_id)
        except Exception as exc:
            state.status = DeviceStatus.ERROR
            state.error_message = str(exc)
            raise RuntimeError(f"Failed to initialize device {device_id}: {exc}") from exc

    async def _start_streaming(self, device_id: str) -> None:
        state = self.devices[device_id]
        loop = asyncio.get_running_loop()

        def _do_start() -> None:
            self._flush_input(state)
            if not state.driver.start_continuous():
                raise RuntimeError("Failed to start continuous mode")

        await loop.run_in_executor(self._executor, _do_start)
        state.streaming_active = True
        logger.info("Device %s continuous mode started", device_id)

    async def _idle_monitor(self) -> None:
        """Sleep streaming devices idle past their configured timeout."""
        check_interval = 30  # seconds
        while True:
            await asyncio.sleep(check_interval)
            for device_id, state in self.devices.items():
                if not state.config.streaming_mode or not state.streaming_active:
                    continue
                timeout_secs = state.config.streaming_idle_timeout * 60
                if timeout_secs <= 0:
                    continue
                idle_secs = time.time() - (state.last_request_time or 0)
                if idle_secs < timeout_secs or state.lock.locked():
                    continue
                async with state.lock:
                    state.status = DeviceStatus.BUSY
                    try:
                        loop = asyncio.get_running_loop()

                        def _do_stop(st: DeviceState = state) -> None:
                            # A RuntimeError here means stop() reached the
                            # device but the ACK read failed on buffered
                            # random bytes — treat as a successful stop.
                            with contextlib.suppress(RuntimeError):
                                st.driver.stop()
                            self._flush_input(st)

                        await loop.run_in_executor(self._executor, _do_stop)
                        logger.info(
                            "Device %s entering sleep mode after %.0fs idle",
                            device_id,
                            idle_secs,
                        )
                    except Exception as exc:
                        logger.warning("Device %s sleep transition error: %s", device_id, exc)
                    finally:
                        state.streaming_active = False  # always — device IS stopped
                        if state.status == DeviceStatus.BUSY:
                            state.status = DeviceStatus.ONLINE

    # ── routing + reads ────────────────────────────────────────────────

    def get_fallback_order(self, primary_device_id: str) -> list[str]:
        """Ordered device list: primary -> same type -> other types."""
        if primary_device_id not in self.devices:
            return list(self.devices.keys())
        primary_type = self.devices[primary_device_id].config.type
        order = [primary_device_id]
        order += [
            dev_id
            for dev_id, state in self.devices.items()
            if dev_id != primary_device_id and state.config.type == primary_type
        ]
        order += [dev_id for dev_id in self.devices if dev_id not in order]
        return order

    async def read_bytes(
        self, primary_device_id: str, num_bytes: int, timeout: float = 5.0
    ) -> tuple[bytes, str]:
        """Read bytes with failover routing.

        Returns ``(data, serving_device_id)``. Raises ``TimeoutError``
        when no device becomes available within *timeout* seconds.
        """
        if self._config.server.failover_enabled:
            fallback_order = self.get_fallback_order(primary_device_id)
        else:
            fallback_order = [primary_device_id]

        start_time = time.monotonic()
        while (time.monotonic() - start_time) < timeout:
            for device_id in fallback_order:
                state = self.devices.get(device_id)
                if not state or state.status not in (DeviceStatus.ONLINE, DeviceStatus.BUSY):
                    continue
                if state.lock.locked():
                    continue
                try:
                    async with state.lock:
                        state.status = DeviceStatus.BUSY
                        try:
                            data = await self._read_from_device(device_id, num_bytes)
                            state.bytes_served += num_bytes
                            state.requests_served += 1
                            state.last_request_time = time.time()
                            return data, device_id
                        finally:
                            if state.status == DeviceStatus.BUSY:
                                state.status = DeviceStatus.ONLINE
                except Exception as exc:
                    state.status = DeviceStatus.ERROR
                    state.error_message = str(exc)
                    logger.error("Device %s read error: %s", device_id, exc)
                    continue  # try next device
            await asyncio.sleep(0.01)

        raise TimeoutError(f"No device available within {timeout} seconds")

    async def _read_from_device(self, device_id: str, num_bytes: int) -> bytes:
        """Read while holding the device lock; reconnects if needed."""
        state = self.devices[device_id]

        if state.driver is None:
            await self._connect_device(device_id)
            if state.config.streaming_mode:
                await self._start_streaming(device_id)

        if state.config.streaming_mode:
            if not state.streaming_active:
                await self._start_streaming(device_id)
                logger.info("Device %s waking from sleep mode", device_id)
            return await self._read_streaming(device_id, num_bytes)

        if num_bytes > state.oneshot_limit:
            return await self._read_large_continuous(device_id, num_bytes)

        loop = asyncio.get_running_loop()

        def _read_oneshot() -> bytes:
            self._flush_input(state)  # freshness: nothing pre-request survives
            data = state.driver.start_one_shot(num_bytes)
            if not data:
                raise RuntimeError("Device returned no data")
            return bytes(data)

        data = await loop.run_in_executor(self._executor, _read_oneshot)
        self._check_length(device_id, data, num_bytes)
        return data

    async def _read_streaming(self, device_id: str, num_bytes: int) -> bytes:
        state = self.devices[device_id]
        loop = asyncio.get_running_loop()

        def _do_read() -> bytes:
            self._flush_input(state)
            data = state.driver.read_continuous(num_bytes)
            if not data:
                raise RuntimeError("Device returned no data in streaming mode")
            return bytes(data)

        data = await loop.run_in_executor(self._executor, _do_read)
        self._check_length(device_id, data, num_bytes)
        return data

    async def _read_large_continuous(self, device_id: str, num_bytes: int) -> bytes:
        """Serve an over-one-shot-limit request via a temporary burst."""
        state = self.devices[device_id]
        loop = asyncio.get_running_loop()

        def _read_continuous() -> bytes:
            self._flush_input(state)
            if not state.driver.start_continuous():
                raise RuntimeError("Failed to start continuous mode")
            try:
                data = state.driver.read_continuous(num_bytes)
                if not data:
                    raise RuntimeError("Device returned no data in continuous mode")
                return bytes(data)
            finally:
                with contextlib.suppress(Exception):
                    state.driver.stop()
                self._flush_input(state)

        data = await loop.run_in_executor(self._executor, _read_continuous)
        self._check_length(device_id, data, num_bytes)
        return data

    @staticmethod
    def _check_length(device_id: str, data: bytes, num_bytes: int) -> None:
        if len(data) < num_bytes:
            raise RuntimeError(
                f"Device {device_id} returned {len(data)} bytes, expected {num_bytes}"
            )

    # ── status ─────────────────────────────────────────────────────────

    def get_device_status(self, device_id: str) -> dict | None:
        state = self.devices.get(device_id)
        if not state:
            return None
        return {
            "id": device_id,
            "type": state.config.type,
            "path": state.config.path,
            "status": state.status.value,
            "post_processing": (
                None  # not applicable: chardev serves whatever the driver DMA delivers
                if state.config.type == "chardev"
                else state.config.post_processing or self._config.post_processing_mode
            ),
            "streaming_mode": state.config.streaming_mode,
            "streaming_active": state.streaming_active,
            "bytes_served": state.bytes_served,
            "requests_served": state.requests_served,
            "last_request_time": state.last_request_time,
            "error_message": state.error_message,
            "is_available": state.is_available,
            # chardev health/freshness snapshot (None for other types):
            "pci_address": state.config.pci_address,
            "last_flushed_bytes": state.last_flushed_bytes,
            "error_present": state.last_error_present,
            "error_bits": state.last_error_bits,
        }

    def get_all_devices_status(self) -> list[dict]:
        return [self.get_device_status(dev_id) for dev_id in self.devices]
