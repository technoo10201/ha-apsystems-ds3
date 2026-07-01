"""Async serial transport for the CC2530 Zigbee Network Processor (Kadsol firmware).

The CC2530 expects `FE LEN CMD0 CMD1 DATA... FCS` frames where:
    LEN  = number of bytes after CMD (i.e. byte length of CMD+DATA minus 2)
    FCS  = XOR of LEN || CMD || DATA

The Kadsol firmware concatenates several frames back-to-back in a single
burst, so a single `recv` call may contain multiple ZNP frames glued together.
We don't try to split them at the byte level — higher layers grep for known
markers (e.g. `44810000`) inside the hex string, exactly like the upstream
firmware does in `AAA_DECODE.ino`.

Reference: `ZIGBEE_HELPERS.ino:5-111`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Final

import serial  # type: ignore[import-untyped]
from serial import SerialException  # type: ignore[import-untyped]

# Home Assistant ships (and from 2026.7 *requires*) the maintained fork
# `pyserial-asyncio-fast`; the API is identical. Fall back to the original
# package so the CLI keeps working in plain virtualenvs that still have it.
try:
    import serial_asyncio_fast as serial_asyncio  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - depends on the environment
    import serial_asyncio  # type: ignore[import-untyped]

_LOGGER = logging.getLogger(__name__)

# `waitSerial2Available` polls for up to 2 s before giving up.
DEFAULT_READ_TIMEOUT_S: Final[float] = 2.0
# After the first byte arrives, the firmware waits an extra ~500 ms for the
# rest of the burst. We keep the same behaviour because several ZNP frames
# arrive back-to-back with sub-frame gaps.
BURST_QUIET_S: Final[float] = 0.5
BAUDRATE: Final[int] = 115200
# `pyserial-asyncio-fast` pushes the whole frame to the fd in one eager
# syscall, while the original `pyserial-asyncio` dribbled it out through the
# event-loop writer. Field observation (July 2026, CH340E + CC2530/CC2591):
# the first long frame written in one burst (the 45-byte NO broadcast) wedges
# the dongle — it stops answering anything until a USB reset. Pacing the
# writes into small chunks with a breather in between reproduces the old
# lib's cadence and keeps the CH340E alive.
WRITE_CHUNK_BYTES: Final[int] = 16
WRITE_INTER_CHUNK_S: Final[float] = 0.002


class ZNPError(Exception):
    """Raised when the CC2530 returns an unexpected or malformed response."""


class ZNPTimeout(ZNPError):
    """Raised when no bytes arrive within the read timeout."""


class ZNP:
    """Asynchronous CC2530 ZNP transport.

    All public methods are safe to call concurrently — they serialise through
    an internal `asyncio.Lock` because the dongle handles one command at a
    time.
    """

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = BAUDRATE,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        burst_quiet_s: float = BURST_QUIET_S,
        dtr_reset: bool = True,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._read_timeout_s = read_timeout_s
        self._burst_quiet_s = burst_quiet_s
        self._dtr_reset = dtr_reset
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def port(self) -> str:
        return self._port

    async def open(self) -> None:
        """Open the serial port. Idempotent.

        Many cheap USB-TTL bridges (CH340E, CP2102, FT232RL) leave DTR / RTS
        asserted by default. On boards where DTR is wired to the CC2530's
        RESET pin (a very common reference build), an asserted DTR holds the
        module **in permanent reset** — every TX is silently swallowed. We
        therefore:

          1. (optional) pulse DTR briefly to do an explicit hardware reset of
             the CC2530 before bringing the async transport up, and
          2. always release DTR + RTS afterwards so the chip can actually run.

        If your bridge doesn't wire DTR to RESET, the toggle is harmless.
        """
        if self._writer is not None:
            return
        # The device can disappear at any point (CH340 hot-unplug, USB reset
        # while we are opening): surface that as ZNPError so callers retry via
        # their normal recovery paths instead of blowing up the setup with an
        # unhandled SerialException (observed in the field, July 2026).
        try:
            if self._dtr_reset:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._pulse_reset
                )
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._port,
                baudrate=self._baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
            )
        except (SerialException, OSError) as err:
            raise ZNPError(f"could not open {self._port}: {err}") from err
        self._release_control_lines()

    def _pulse_reset(self) -> None:
        """Synchronous DTR pulse (blocking; called from an executor)."""
        with serial.Serial(self._port, self._baudrate) as ser:
            ser.dtr = True
            time.sleep(0.05)
            ser.dtr = False
            time.sleep(0.5)
            ser.reset_input_buffer()

    def _release_control_lines(self) -> None:
        """Make sure DTR/RTS are inactive on the open transport.

        `serial_asyncio` re-opens the underlying `serial.Serial` and may
        re-assert DTR/RTS depending on the platform; we explicitly clear
        them so the CC2530 isn't held in reset for the rest of the session.
        """
        if self._writer is None:
            return
        transport = self._writer.transport
        ser = getattr(transport, "serial", None)
        if ser is None:
            return
        try:
            ser.dtr = False
            ser.rts = False
        except (AttributeError, SerialException, OSError):
            # Not every backend supports tweaking the lines after open —
            # logging at debug only; not fatal.
            _LOGGER.debug("could not release DTR/RTS on %s", self._port)

    async def close(self) -> None:
        """Close the serial port. Idempotent."""
        if self._writer is None:
            return
        try:
            self._writer.close()
            await self._writer.wait_closed()
        finally:
            self._reader = None
            self._writer = None

    async def __aenter__(self) -> ZNP:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def is_open(self) -> bool:
        """True if both halves of the transport are live."""
        return self._writer is not None and self._reader is not None

    async def send(self, payload_hex: str) -> None:
        """Frame and send a ZNP payload (CMD+DATA), without reading the answer."""
        if self._writer is None:
            raise ZNPError("ZNP transport not open")
        frame = _build_frame(payload_hex)
        async with self._lock:
            await self._drain_input()
            await self._write_frame(frame)
        _LOGGER.debug("ZNP TX %s", frame.hex().upper())

    async def recv(self) -> str:
        """Read whatever the dongle has queued up.

        Returns an uppercase hex string of all bytes received in the current
        burst (possibly several concatenated ZNP frames). Returns an empty
        string on timeout — callers decide whether that is fatal.
        """
        if self._reader is None:
            raise ZNPError("ZNP transport not open")
        async with self._lock:
            return await self._read_burst()

    async def request(self, payload_hex: str) -> str:
        """Send `payload_hex` then read the answer burst as a hex string."""
        if self._writer is None or self._reader is None:
            raise ZNPError("ZNP transport not open")
        frame = _build_frame(payload_hex)
        async with self._lock:
            await self._drain_input()
            await self._write_frame(frame)
            _LOGGER.debug("ZNP TX %s", frame.hex().upper())
            return await self._read_burst()

    async def _write_frame(self, frame: bytes) -> None:
        """Write `frame` in small paced chunks (see WRITE_CHUNK_BYTES note)."""
        assert self._writer is not None
        try:
            for offset in range(0, len(frame), WRITE_CHUNK_BYTES):
                self._writer.write(frame[offset : offset + WRITE_CHUNK_BYTES])
                await self._writer.drain()
                if offset + WRITE_CHUNK_BYTES < len(frame):
                    await asyncio.sleep(WRITE_INTER_CHUNK_S)
        except (SerialException, OSError, ConnectionError) as err:
            await self._mark_disconnected()
            raise ZNPError(f"transport write failed: {err}") from err

    async def _drain_input(self) -> None:
        """Discard whatever bytes are pending without blocking."""
        assert self._reader is not None
        try:
            while True:
                chunk = await asyncio.wait_for(self._reader.read(256), timeout=0.01)
                if not chunk:
                    return
        except TimeoutError:
            return
        except (SerialException, OSError, ConnectionError) as err:
            await self._mark_disconnected()
            raise ZNPError(f"transport drain failed: {err}") from err

    async def _read_burst(self) -> str:
        """Read until either the timeout expires or the line goes quiet."""
        assert self._reader is not None
        buf = bytearray()
        try:
            first = await asyncio.wait_for(self._reader.read(1024), timeout=self._read_timeout_s)
        except TimeoutError:
            return ""
        except (SerialException, OSError, ConnectionError) as err:
            await self._mark_disconnected()
            raise ZNPError(f"transport read failed: {err}") from err
        if not first:
            return ""
        buf.extend(first)
        # Keep draining the burst until it goes quiet.
        while True:
            try:
                more = await asyncio.wait_for(self._reader.read(1024), timeout=self._burst_quiet_s)
            except TimeoutError:
                break
            except (SerialException, OSError, ConnectionError) as err:
                await self._mark_disconnected()
                raise ZNPError(f"transport read failed mid-burst: {err}") from err
            if not more:
                break
            buf.extend(more)
        hex_str = buf.hex().upper()
        # The firmware (`ZIGBEE_HELPERS.ino:42-45`) reports the dongle
        # occasionally prepends a stray `0xF8` byte that must be skipped.
        if hex_str.startswith("F8"):
            hex_str = hex_str[2:]
        _LOGGER.debug("ZNP RX %s", hex_str)
        return hex_str

    async def _mark_disconnected(self) -> None:
        """Tear down the transport so callers know to reopen.

        We don't try to recover here — that is the coordinator's job (see
        `coordinator._async_recover`). The contract is: after this call,
        `is_open` is False and the next `request` will raise ZNPError until
        someone calls `open()` again.
        """
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


def _build_frame(payload_hex: str) -> bytes:
    """Wrap a CMD+DATA hex payload into the wire frame `FE LEN CMD DATA FCS`.

    Reference: `ZIGBEE_HELPERS.ino:58-90` (`sendZB`) and `:95-111`
    (`checkSumString`).
    """
    payload = bytes.fromhex(payload_hex)
    if len(payload) < 2:
        raise ValueError(f"payload must contain at least CMD0+CMD1, got {payload_hex!r}")
    length = len(payload) - 2
    if not 0 <= length <= 0xFF:
        raise ValueError(f"payload too long: {len(payload)} bytes")
    body = bytes([length]) + payload
    fcs = 0
    for b in body:
        fcs ^= b
    return b"\xfe" + body + bytes([fcs])


def compute_fcs(payload_hex: str) -> int:
    """Standalone XOR-FCS helper used by tests."""
    payload = bytes.fromhex(payload_hex)
    length = len(payload) - 2
    fcs = length
    for b in payload:
        fcs ^= b
    return fcs & 0xFF
