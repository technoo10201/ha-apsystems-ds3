"""Tests for the poll pipeline, focused on the serial-identity guard.

The AF_INCOMING_MSG of a poll answer carries the responding inverter's own
serial. `poll_inverter(expected_serial=...)` must reject an answer whose
serial differs from the one the config maps to the polled short address —
that is the symptom of two inverters with swapped invIDs (seen in the field
with manual invID entry), which would otherwise silently cross-attribute
the production data of the two units.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.aps_zigbee.aps_protocol.polling import (
    PollError,
    SerialMismatchError,
    poll_inverter,
)

# Same verbatim firmware trace as test_decode_ds3.DS3_FIXTURE — a complete
# successful poll burst whose payload serial is 703000021300.
_BURST = (
    "FE0164010064"
    "FE034480001401D2"
    "FE0345C43A1000A8"
    "FE724481000006013A101414007100B57CFA00005E703000021300"
    "fbfb5cbbbb20000200e6ffff000000000000000006f506f9002e00340360138a17a70024001fffff"
    "054206900016f62b0018e451"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "3969fefe"
)
_BURST_SERIAL = "703000021300"


class _FakeZNP:
    """Minimal stand-in for ZNP: returns a canned burst on request()."""

    def __init__(self, burst: str) -> None:
        self._burst = burst

    async def request(self, cmd: str) -> str:
        return self._burst


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_poll_accepts_matching_expected_serial() -> None:
    result = _run(
        poll_inverter(_FakeZNP(_BURST), "103A", "D8A3011B9780",
                      expected_serial=_BURST_SERIAL)
    )
    assert result.reading.serial == _BURST_SERIAL


def test_poll_without_expected_serial_keeps_old_behaviour() -> None:
    result = _run(poll_inverter(_FakeZNP(_BURST), "103A", "D8A3011B9780"))
    assert result.reading.serial == _BURST_SERIAL


def test_poll_extracts_route_from_burst() -> None:
    # _BURST contains "FE0345C43A1000A8" = ZDO_SRC_RTG_IND for dst 0x103A
    # with 0 relays → direct radio link.
    result = _run(poll_inverter(_FakeZNP(_BURST), "103A", "D8A3011B9780"))
    assert result.relays == []


def test_poll_rejects_mismatched_serial() -> None:
    with pytest.raises(SerialMismatchError) as exc_info:
        _run(
            poll_inverter(_FakeZNP(_BURST), "103A", "D8A3011B9780",
                          expected_serial="704000111111")
        )
    # The message must name both serials and hint at the invID mapping so the
    # HA log alone is enough to understand and fix the swap.
    msg = str(exc_info.value)
    assert _BURST_SERIAL in msg
    assert "704000111111" in msg
    assert "invID" in msg


def test_serial_mismatch_is_a_poll_error() -> None:
    # The coordinator catches PollError — the new guard must stay inside
    # that hierarchy so existing failure handling keeps working.
    assert issubclass(SerialMismatchError, PollError)
