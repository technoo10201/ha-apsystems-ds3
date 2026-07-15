"""Coordinator recovery robustness: re-entrancy guard + slow retry after exhaustion.

The real `homeassistant` package is not installed in the test venv (the rest of
the suite is protocol-level), so the four runtime imports of
`custom_components.aps_zigbee.coordinator` are stubbed with minimal fakes
before the module is imported.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.ha_stubs import install_ha_stubs

# --------------------------------------------------------------------- HA stubs

install_ha_stubs()

from custom_components.aps_zigbee import coordinator as coordinator_mod  # noqa: E402
from custom_components.aps_zigbee.aps_protocol.frames import (  # noqa: E402
    COORDINATOR_SHORT_ADDR,
    DEVICE_STATE_ZB_COORD,
    CoordinatorDeviceInfo,
)
from custom_components.aps_zigbee.const import (  # noqa: E402
    CONF_ECU_ID,
    CONF_PORT,
    RECOVERY_IDLE_RETRY_S,
    RECOVERY_RETRIES,
)
from custom_components.aps_zigbee.coordinator import (  # noqa: E402
    APSDataUpdateCoordinator,
    _utcnow,
)

# --------------------------------------------------------------------- helpers

_HEALTHY_INFO = CoordinatorDeviceInfo(
    status=0,
    ieee="D8A3011B9780FFFF",
    short_addr=COORDINATOR_SHORT_ADDR,
    device_type=7,
    device_state=DEVICE_STATE_ZB_COORD,
    num_assoc=0,
)

_DEV_HOLD_INFO = CoordinatorDeviceInfo(
    status=0,
    ieee="D8A3011B9780FFFF",
    short_addr="FFFE",
    device_type=7,
    device_state=0x00,
    num_assoc=0,
)

# --------------------------------------------------------------------- fixtures


def _make_coordinator() -> APSDataUpdateCoordinator:
    hass = MagicMock(name="hass")
    hass.loop = asyncio.get_event_loop()
    entry = MagicMock(name="entry")
    entry.data = {CONF_ECU_ID: "D8A3011B9780", CONF_PORT: "/dev/null"}
    entry.options = {}
    coord = APSDataUpdateCoordinator(hass, entry)
    coord.znp = AsyncMock(name="znp")
    return coord


def _instant_sleep():
    return patch.object(coordinator_mod.asyncio, "sleep", new=AsyncMock())


# --------------------------------------------------------------------- tests


async def test_concurrent_recover_calls_coalesce() -> None:
    """A second _async_recover while one is in flight must return immediately."""
    coord = _make_coordinator()
    gate = asyncio.Event()
    real_sleep = asyncio.sleep  # keep a handle: asyncio.sleep gets patched below

    async def blocked_check(znp):
        await gate.wait()
        return _HEALTHY_INFO

    with (
        _instant_sleep(),
        patch.object(coordinator_mod, "check_network", side_effect=blocked_check)
        as check_mock,
    ):
        task1 = asyncio.ensure_future(coord._async_recover())
        await real_sleep(0)  # let task1 reach the gate
        assert coord._recovering is True

        # Duplicate request coalesces: returns without touching the port again.
        await coord._async_recover()
        assert check_mock.call_count == 1
        assert coord.znp.close.await_count == 1

        gate.set()
        await task1

    assert coord._init_done is True
    assert coord._recovering is False
    assert check_mock.call_count == 1


async def test_exhausted_recovery_resets_guard_and_timestamp() -> None:
    """After RECOVERY_RETRIES failures the guard is released and the end recorded."""
    coord = _make_coordinator()
    coord.znp.open.side_effect = coordinator_mod.ZNPError("port gone")

    with _instant_sleep():
        await coord._async_recover()

    assert coord.znp.open.await_count == RECOVERY_RETRIES
    assert coord._init_done is False
    assert coord._recovering is False
    assert coord._last_recovery_end is not None


async def test_watchdog_retries_after_cooldown() -> None:
    """With the coordinator down and the cooldown elapsed, the watchdog retries."""
    coord = _make_coordinator()
    coord._init_done = False
    coord._last_recovery_end = _utcnow() - timedelta(seconds=RECOVERY_IDLE_RETRY_S + 1)
    coord._async_recover = AsyncMock(name="_async_recover")

    # First tick proceeds, second tick aborts the infinite loop.
    with patch.object(
        coordinator_mod.asyncio,
        "sleep",
        new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await coord._watchdog_loop()

    assert coord._async_recover.await_count == 1


async def test_watchdog_respects_cooldown() -> None:
    """A fresh (failed) recovery must not be retried before the cooldown."""
    coord = _make_coordinator()
    coord._init_done = False
    coord._last_recovery_end = _utcnow()
    coord._async_recover = AsyncMock(name="_async_recover")

    with patch.object(
        coordinator_mod.asyncio,
        "sleep",
        new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await coord._watchdog_loop()

    assert coord._async_recover.await_count == 0


async def test_watchdog_skips_while_recovery_in_flight() -> None:
    """No duplicate retry is scheduled while _async_recover is already running."""
    coord = _make_coordinator()
    coord._init_done = False
    coord._recovering = True
    coord._last_recovery_end = None  # would otherwise retry immediately
    coord._async_recover = AsyncMock(name="_async_recover")

    with patch.object(
        coordinator_mod.asyncio,
        "sleep",
        new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await coord._watchdog_loop()

    assert coord._async_recover.await_count == 0


# --------------------------------------------------------------------- soft/hard recovery dispatch


async def test_soft_recovery_trusts_healthy_network() -> None:
    """When check_network reports network_up=True, init_coordinator is never called."""
    coord = _make_coordinator()

    with (
        _instant_sleep(),
        patch.object(coordinator_mod, "check_network", return_value=_HEALTHY_INFO),
        patch.object(
            coordinator_mod, "init_coordinator", new=AsyncMock()
        ) as init_mock,
    ):
        await coord._async_recover()

    assert coord._init_done is True
    assert coord._recovering is False
    init_mock.assert_not_awaited()


async def test_soft_recovery_escalates_to_hard_on_dev_hold() -> None:
    """DEV_HOLD after reopen (serial answers, network down) → init_coordinator runs."""
    coord = _make_coordinator()

    with (
        _instant_sleep(),
        patch.object(coordinator_mod, "check_network", return_value=_DEV_HOLD_INFO),
        patch.object(
            coordinator_mod, "init_coordinator", new=AsyncMock(return_value=True)
        ) as init_mock,
    ):
        await coord._async_recover()

    assert coord._init_done is True
    assert coord._recovering is False
    init_mock.assert_awaited_once()


# --------------------------------------------------------------------- watchdog dispatch


async def test_watchdog_no_recovery_when_network_up() -> None:
    """Healthy check_network response must not trigger recovery."""
    coord = _make_coordinator()
    coord._init_done = True
    coord._async_recover = AsyncMock(name="_async_recover")

    with (
        patch.object(coordinator_mod, "check_network", return_value=_HEALTHY_INFO),
        patch.object(
            coordinator_mod.asyncio,
            "sleep",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await coord._watchdog_loop()

    coord._async_recover.assert_not_awaited()


async def test_watchdog_recovers_on_dev_hold_despite_serial_alive() -> None:
    """DEV_HOLD from watchdog check triggers recovery and marks update as failed."""
    coord = _make_coordinator()
    coord._init_done = True
    coord._async_recover = AsyncMock(name="_async_recover")

    with (
        patch.object(coordinator_mod, "check_network", return_value=_DEV_HOLD_INFO),
        patch.object(
            coordinator_mod.asyncio,
            "sleep",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await coord._watchdog_loop()

    coord._async_recover.assert_awaited_once()
    assert coord.last_update_success is False
