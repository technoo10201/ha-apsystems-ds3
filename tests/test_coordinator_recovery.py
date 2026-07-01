"""Coordinator recovery robustness: re-entrancy guard + slow retry after exhaustion.

The real `homeassistant` package is not installed in the test venv (the rest of
the suite is protocol-level), so the four runtime imports of
`custom_components.aps_zigbee.coordinator` are stubbed with minimal fakes
before the module is imported.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --------------------------------------------------------------------- HA stubs


def _install_ha_stubs() -> None:
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")

    exceptions = types.ModuleType("homeassistant.exceptions")

    class ConfigEntryNotReady(Exception):
        pass

    exceptions.ConfigEntryNotReady = ConfigEntryNotReady

    helpers = types.ModuleType("homeassistant.helpers")
    device_registry = MagicMock(name="homeassistant.helpers.device_registry")
    sun = types.ModuleType("homeassistant.helpers.sun")
    sun.is_up = lambda hass: True

    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        """Bare-bones stand-in exposing only what APSDataUpdateCoordinator uses."""

        def __class_getitem__(cls, item):
            return cls

        def __init__(self, hass, logger, *, name, update_interval):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.last_update_success = True
            # NB: pas de `last_update_success_time` ici — APSDataUpdateCoordinator
            # définit sa propre property read-only du même nom.

        async def async_request_refresh(self) -> None:
            return None

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.exceptions"] = exceptions
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.device_registry"] = device_registry
    sys.modules["homeassistant.helpers.sun"] = sun
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator


_install_ha_stubs()

from custom_components.aps_zigbee import coordinator as coordinator_mod  # noqa: E402
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
        return True

    with (
        _instant_sleep(),
        patch.object(coordinator_mod, "check_coordinator", side_effect=blocked_check)
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
