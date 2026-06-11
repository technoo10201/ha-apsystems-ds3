"""APsystems Zigbee (CC2530 direct) — Home Assistant entry points.

Top-level imports are kept HA-free so the pure protocol layer in `aps_protocol`
stays importable (and testable) without Home Assistant installed. Anything
that needs `homeassistant` is loaded inside the entry-point functions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

# Imported on the HA side only.
_SERIAL_RE = r"^\d{12}$"


_PLATFORMS_STR = ("sensor", "button")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    import asyncio

    from homeassistant.const import Platform
    from homeassistant.exceptions import ConfigEntryNotReady

    from .aps_protocol.znp import ZNPError
    from .const import DOMAIN, STARTUP_RETRIES, STARTUP_RETRY_DELAY_S
    from .coordinator import APSDataUpdateCoordinator

    coordinator = APSDataUpdateCoordinator(hass, entry)

    last_err: Exception | None = None
    for attempt in range(STARTUP_RETRIES):
        try:
            await coordinator.async_open()
            break
        except (ZNPError, ConfigEntryNotReady, OSError) as err:
            last_err = err
            _LOGGER.warning(
                "startup attempt %s/%s failed: %s",
                attempt + 1,
                STARTUP_RETRIES,
                err,
            )
            await asyncio.sleep(STARTUP_RETRY_DELAY_S)
    else:
        raise ConfigEntryNotReady(f"coordinator init failed: {last_err}") from last_err

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    platforms = [Platform(p) for p in _PLATFORMS_STR]
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    coordinator.start_watchdog()
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down the integration."""
    from homeassistant.const import Platform

    from .const import (
        DOMAIN,
        SERVICE_CANCEL_REBIND,
        SERVICE_DISCOVER,
        SERVICE_PAIR_INVERTER,
        SERVICE_REBIND_PERSISTENT,
        SERVICE_REBOOT_INVERTER,
        SERVICE_REPOLL,
    )
    from .coordinator import APSDataUpdateCoordinator

    platforms = [Platform(p) for p in _PLATFORMS_STR]
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
    coordinator: APSDataUpdateCoordinator | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_close()
    if not hass.data.get(DOMAIN):
        for service in (
            SERVICE_PAIR_INVERTER,
            SERVICE_REPOLL,
            SERVICE_REBOOT_INVERTER,
            SERVICE_DISCOVER,
            SERVICE_REBIND_PERSISTENT,
            SERVICE_CANCEL_REBIND,
        ):
            hass.services.async_remove(DOMAIN, service)
        hass.data.pop(DOMAIN, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register the three integration-level services once."""
    import voluptuous as vol
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import config_validation as cv

    from .aps_protocol.pairing import PairingError
    from .aps_protocol.polling import PollError
    from .const import (
        ATTR_DURATION_MIN,
        ATTR_NAME,
        ATTR_PAUSE_S,
        ATTR_SERIAL,
        CAMPAIGN_DURATION_DEFAULT_MIN,
        CAMPAIGN_DURATION_MAX_MIN,
        CAMPAIGN_PAUSE_DEFAULT_S,
        CAMPAIGN_PAUSE_MAX_S,
        CAMPAIGN_PAUSE_MIN_S,
        DOMAIN,
        SERVICE_CANCEL_REBIND,
        SERVICE_DISCOVER,
        SERVICE_PAIR_INVERTER,
        SERVICE_REBIND_PERSISTENT,
        SERVICE_REBOOT_INVERTER,
        SERVICE_REPOLL,
    )
    from .coordinator import APSDataUpdateCoordinator

    if hass.services.has_service(DOMAIN, SERVICE_PAIR_INVERTER):
        return

    pair_schema = vol.Schema(
        {
            vol.Required(ATTR_SERIAL): cv.matches_regex(_SERIAL_RE),
            vol.Optional(ATTR_NAME, default=""): cv.string,
        }
    )
    repoll_schema = vol.Schema({vol.Optional(ATTR_SERIAL): cv.matches_regex(_SERIAL_RE)})
    reboot_schema = vol.Schema({vol.Required(ATTR_SERIAL): cv.matches_regex(_SERIAL_RE)})
    rebind_persistent_schema = vol.Schema(
        {
            vol.Required(ATTR_SERIAL): cv.matches_regex(_SERIAL_RE),
            vol.Optional(
                ATTR_DURATION_MIN, default=CAMPAIGN_DURATION_DEFAULT_MIN
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=CAMPAIGN_DURATION_MAX_MIN)),
            vol.Optional(ATTR_PAUSE_S, default=CAMPAIGN_PAUSE_DEFAULT_S): vol.All(
                vol.Coerce(int),
                vol.Range(min=CAMPAIGN_PAUSE_MIN_S, max=CAMPAIGN_PAUSE_MAX_S),
            ),
        }
    )

    def _pick_coordinator() -> APSDataUpdateCoordinator:
        coordinators: dict[str, APSDataUpdateCoordinator] = hass.data[DOMAIN]
        if not coordinators:
            raise HomeAssistantError("No APS Zigbee integration is loaded")
        return next(iter(coordinators.values()))

    async def _pair(call: ServiceCall) -> None:
        coordinator = _pick_coordinator()
        serial = call.data[ATTR_SERIAL]
        name = call.data.get(ATTR_NAME) or f"APS DS3 {serial[-4:]}"
        try:
            await coordinator.async_pair_new_inverter(serial, name)
        except PairingError as err:
            raise HomeAssistantError(str(err)) from err

    async def _repoll(call: ServiceCall) -> None:
        coordinator = _pick_coordinator()
        await coordinator.async_request_refresh()

    async def _reboot(call: ServiceCall) -> None:
        coordinator = _pick_coordinator()
        try:
            await coordinator.async_reboot_inverter(call.data[ATTR_SERIAL])
        except (KeyError, PollError) as err:
            raise HomeAssistantError(str(err)) from err

    async def _discover(call: ServiceCall) -> None:
        coordinator = _pick_coordinator()
        await coordinator.async_discover()

    async def _rebind_persistent(call: ServiceCall) -> None:
        coordinator = _pick_coordinator()
        try:
            await coordinator.async_start_rebind_campaign(
                call.data[ATTR_SERIAL],
                call.data[ATTR_DURATION_MIN],
                call.data[ATTR_PAUSE_S],
            )
        except PairingError as err:
            raise HomeAssistantError(str(err)) from err

    async def _cancel_rebind(call: ServiceCall) -> None:
        coordinator = _pick_coordinator()
        await coordinator.async_cancel_rebind_campaign()

    hass.services.async_register(DOMAIN, SERVICE_PAIR_INVERTER, _pair, schema=pair_schema)
    hass.services.async_register(DOMAIN, SERVICE_REPOLL, _repoll, schema=repoll_schema)
    hass.services.async_register(DOMAIN, SERVICE_REBOOT_INVERTER, _reboot, schema=reboot_schema)
    hass.services.async_register(DOMAIN, SERVICE_DISCOVER, _discover)
    hass.services.async_register(
        DOMAIN,
        SERVICE_REBIND_PERSISTENT,
        _rebind_persistent,
        schema=rebind_persistent_schema,
    )
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_REBIND, _cancel_rebind)
