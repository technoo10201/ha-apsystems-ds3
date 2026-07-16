"""Tests for sensor availability when inverters transition to DEAD.

Covers three failure modes:
  - Daytime failures accumulate to DEAD → available becomes False and stays False
    even during subsequent carry-forward cycles.
  - A DEAD inverter stays unavailable through the night (DEAD latches).
  - _carry_forward overwrites a stale `available: True` with the authoritative
    value derived from the runtime state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.aps_zigbee import coordinator as coordinator_mod  # noqa: E402
from custom_components.aps_zigbee.aps_protocol.runtime import (  # noqa: E402
    InverterRuntime,
    InverterState,
)
from custom_components.aps_zigbee.const import (  # noqa: E402
    BACKOFF_BASE_S,
    BACKOFF_MAX_S,
    CONF_ECU_ID,
    CONF_INVERTERS,
    CONF_PORT,
    DEAD_THRESHOLD,
    INV_ID,
    INV_NAME,
    INV_SERIAL,
)
from custom_components.aps_zigbee.coordinator import APSDataUpdateCoordinator  # noqa: E402

_T0 = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)

_SERIAL = "408000158211"
_INV_ID = "B745"


def _make_coordinator() -> APSDataUpdateCoordinator:
    hass = MagicMock(name="hass")
    hass.loop = asyncio.get_event_loop()
    entry = MagicMock(name="entry")
    entry.data = {
        CONF_ECU_ID: "D8A3011B9780",
        CONF_PORT: "/dev/null",
        CONF_INVERTERS: [{INV_SERIAL: _SERIAL, INV_ID: _INV_ID, INV_NAME: "test"}],
    }
    entry.options = {}
    coord = APSDataUpdateCoordinator(hass, entry)
    coord.znp = MagicMock(name="znp")
    coord.znp.is_open = True
    coord._init_done = True
    return coord


async def test_carry_forward_availability_is_authoritative() -> None:
    """_carry_forward must overwrite a stale `available: True` when state is DEAD."""
    coord = _make_coordinator()
    runtime = coord._runtime(_SERIAL)
    # Force the inverter into DEAD state
    runtime.state = InverterState.DEAD
    # Set a future retry so carry-forward is taken (should_skip = True)
    runtime.next_retry_after = _T0 + timedelta(hours=1)

    # Pre-populate coord.data with a stale available: True (the incident scenario)
    coord.data = {_SERIAL: {"available": True, "state": "ok"}}

    payload = coord._carry_forward(_SERIAL, runtime)
    assert payload["available"] is False


async def test_dead_state_latches_through_night() -> None:
    """A DEAD inverter stays unavailable when further failures arrive at night."""
    coord = _make_coordinator()
    runtime = coord._runtime(_SERIAL)

    # Kill the inverter during the day
    for _ in range(DEAD_THRESHOLD):
        runtime.record_failure(
            _T0,
            sun_is_up=True,
            dead_threshold=DEAD_THRESHOLD,
            base_s=BACKOFF_BASE_S,
            cap_s=BACKOFF_MAX_S,
        )
    assert runtime.state is InverterState.DEAD

    # Night failure: must not revive to IDLE
    night = _T0 + timedelta(hours=9)
    runtime.record_failure(
        night,
        sun_is_up=False,
        dead_threshold=DEAD_THRESHOLD,
        base_s=BACKOFF_BASE_S,
        cap_s=BACKOFF_MAX_S,
    )
    assert runtime.state is InverterState.DEAD

    payload = coord._render_failure(_SERIAL, runtime)
    assert payload["available"] is False


async def test_sensors_unavailable_after_dead_threshold_daytime() -> None:
    """After DEAD_THRESHOLD daytime poll failures available=False and stays False."""
    from custom_components.aps_zigbee.aps_protocol.polling import PollError

    coord = _make_coordinator()

    # Advance clock by 60 s per call to clear backoff windows (max backoff = 5 min,
    # but each _async_update_data sees a *different* 'now', 60 s ahead of the last).
    tick = [0]

    def fake_now():
        t = _T0 + timedelta(seconds=tick[0] * 60)
        tick[0] += 1
        return t

    with (
        patch.object(coordinator_mod, "_utcnow", side_effect=fake_now),
        patch.object(coordinator_mod, "is_up", return_value=True),
        patch.object(
            coordinator_mod,
            "poll_inverter",
            new=AsyncMock(side_effect=PollError("timeout")),
        ),
    ):
        # Run enough cycles for the inverter to reach DEAD.
        # We need DEAD_THRESHOLD failures counted (not skipped by backoff).
        # With 60 s advances, early backoffs (1 s, 2 s, 4 s, 8 s, 16 s) expire
        # before the next tick, but later ones (32 s, 64→cap=300 s) may not.
        # Strategy: run many cycles; only the ones that bypass should_skip count.
        results = {}
        for _ in range(DEAD_THRESHOLD * 4):
            results = await coord._async_update_data()

    assert results[_SERIAL]["available"] is False

    # On the next carry-forward cycle (backoff still active), available stays False.
    runtime = coord._runtime(_SERIAL)
    assert runtime.state is InverterState.DEAD
    coord.data = results
    # Set a future retry to force carry-forward
    runtime.next_retry_after = fake_now() + timedelta(hours=1)
    payload = coord._carry_forward(_SERIAL, runtime)
    assert payload["available"] is False
