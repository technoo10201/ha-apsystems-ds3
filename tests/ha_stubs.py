"""Minimal homeassistant stubs for the protocol-level test suite.

Call `install_ha_stubs()` **before** importing any
`custom_components.aps_zigbee` module that itself imports from homeassistant.
The function is idempotent: it returns immediately if the stubs are already
registered in `sys.modules`.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def install_ha_stubs() -> None:
    """Register minimal homeassistant fakes into sys.modules."""
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
            self.last_exception = None
            self.data = None
            # NB: no `last_update_success_time` here — APSDataUpdateCoordinator
            # defines its own read-only property of the same name.

        async def async_request_refresh(self) -> None:
            return None

        def async_set_update_error(self, err) -> None:
            self.last_update_success = False
            self.last_exception = err

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.exceptions"] = exceptions
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.device_registry"] = device_registry
    sys.modules["homeassistant.helpers.sun"] = sun
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
