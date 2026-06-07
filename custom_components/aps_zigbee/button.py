"""Button platform — per-inverter Refresh + Reboot buttons.

Refresh triggers an on-demand poll of that single inverter (bypassing the
backoff window) and pushes the result into the coordinator's data so the
device's sensors update immediately. Reboot sends the proprietary reboot
command (same effect as the `aps_zigbee.reboot_inverter` service).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .aps_protocol.polling import PollError
from .aps_protocol.znp import ZNPError
from .const import DOMAIN, INV_ID, INV_NAME, INV_SERIAL, MANUFACTURER, MODEL
from .coordinator import APSDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


@dataclass(frozen=True, kw_only=True)
class APSButtonDescription(ButtonEntityDescription):
    """Button description tagged with the action to perform."""

    action: str  # "refresh" | "reboot"


_BUTTONS: tuple[APSButtonDescription, ...] = (
    APSButtonDescription(
        key="refresh",
        translation_key="refresh",
        action="refresh",
        icon="mdi:refresh",
    ),
    APSButtonDescription(
        key="reboot",
        translation_key="reboot",
        action="reboot",
        device_class=ButtonDeviceClass.RESTART,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: APSDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[APSButton] = []
    for inv in coordinator.inverters:
        for desc in _BUTTONS:
            entities.append(APSButton(coordinator, inv, desc))
    async_add_entities(entities)


class APSButton(ButtonEntity):
    """One action button bound to a single paired inverter."""

    _attr_has_entity_name = True
    entity_description: APSButtonDescription

    def __init__(
        self,
        coordinator: APSDataUpdateCoordinator,
        inverter: dict[str, str],
        description: APSButtonDescription,
    ) -> None:
        self._coordinator = coordinator
        self.entity_description = description
        self._serial = inverter[INV_SERIAL]
        self._attr_unique_id = f"{self._serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=inverter.get(INV_NAME) or f"APS DS3 {self._serial[-4:]}",
            serial_number=self._serial,
            sw_version=inverter.get(INV_ID),
        )

    async def async_press(self) -> None:
        action = self.entity_description.action
        try:
            if action == "refresh":
                await self._coordinator.async_repoll_inverter(self._serial)
            elif action == "reboot":
                await self._coordinator.async_reboot_inverter(self._serial)
            else:  # pragma: no cover - guarded by the descriptor enum
                raise HomeAssistantError(f"unknown button action {action!r}")
        except (PollError, ZNPError, KeyError) as err:
            raise HomeAssistantError(str(err)) from err
