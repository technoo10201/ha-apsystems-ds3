"""Verify ZNP marks itself disconnected on hot-unplug events."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from serial import SerialException  # type: ignore[import-untyped]

from custom_components.aps_zigbee.aps_protocol.znp import ZNP, ZNPError


def _make_open_znp() -> ZNP:
    """Build a ZNP with sentinel reader/writer simulating an open transport."""
    znp = ZNP("/dev/null")
    znp._reader = MagicMock()
    # `_drain_input` reads from the reader before each write — return empty
    # bytes immediately so we get past the drain step.
    znp._reader.read = AsyncMock(return_value=b"")
    znp._writer = MagicMock()
    znp._writer.close = MagicMock()
    znp._writer.drain = AsyncMock()
    return znp


async def test_request_marks_disconnected_on_write_error() -> None:
    znp = _make_open_znp()
    znp._writer.write = MagicMock(side_effect=SerialException("device removed"))

    with pytest.raises(ZNPError, match="write failed"):
        await znp.request("2600")

    assert not znp.is_open


async def test_read_burst_marks_disconnected_on_serial_error() -> None:
    znp = _make_open_znp()
    znp._writer.write = MagicMock()
    # First read = drain (returns empty) ; second read = burst (raises).
    znp._reader.read = AsyncMock(side_effect=[b"", SerialException("read EIO")])

    with pytest.raises(ZNPError, match="read failed"):
        await znp.request("2600")

    assert not znp.is_open


async def test_read_burst_marks_disconnected_on_os_error() -> None:
    znp = _make_open_znp()
    znp._writer.write = MagicMock()
    znp._reader.read = AsyncMock(side_effect=[b"", OSError("device disappeared")])

    with pytest.raises(ZNPError):
        await znp.request("2600")

    assert not znp.is_open


async def test_subsequent_call_after_disconnect_raises_not_open() -> None:
    znp = _make_open_znp()
    znp._writer.write = MagicMock(side_effect=SerialException("gone"))

    with pytest.raises(ZNPError):
        await znp.request("2600")

    with pytest.raises(ZNPError, match="not open"):
        await znp.send("2600")


async def test_close_is_idempotent_after_disconnect() -> None:
    znp = _make_open_znp()
    znp._writer.write = MagicMock(side_effect=SerialException("gone"))

    with pytest.raises(ZNPError):
        await znp.request("2600")

    # Already disconnected; close() should be a no-op rather than crash.
    await znp.close()
    assert not znp.is_open


async def test_is_open_reflects_state() -> None:
    znp = ZNP("/dev/null")
    assert not znp.is_open
    znp._reader = MagicMock()
    znp._writer = MagicMock()
    assert znp.is_open
    znp._writer = None
    assert not znp.is_open
