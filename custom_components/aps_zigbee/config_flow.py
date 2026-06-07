"""Config + options flow for the APsystems Zigbee integration.

Initial setup gathers the serial port, the ECU id and the polling interval —
no inverter pairing yet, because pairing needs a running coordinator that we
only spin up in `async_setup_entry`. Inverters are added later through the
options flow (or the `aps_zigbee.pair_inverter` service).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .aps_protocol.pairing import PairingError
from .const import (
    CONF_DTR_RESET,
    CONF_ECU_ID,
    CONF_INVERTERS,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEFAULT_DTR_RESET,
    DEFAULT_ECU_ID,
    DEFAULT_SCAN_INTERVAL_S,
    DOMAIN,
    INV_ID,
    INV_NAME,
    INV_SERIAL,
    MAX_SCAN_INTERVAL_S,
    MIN_SCAN_INTERVAL_S,
)

if TYPE_CHECKING:
    pass

_ECU_RE = re.compile(r"^[0-9A-Fa-f]{12}$")
_SERIAL_RE = re.compile(r"^\d{12}$")


class APSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            port = user_input[CONF_PORT].strip()
            ecu_id = user_input[CONF_ECU_ID].strip().upper()
            scan_interval = user_input[CONF_SCAN_INTERVAL]
            dtr_reset = user_input.get(CONF_DTR_RESET, DEFAULT_DTR_RESET)
            if not _ECU_RE.match(ecu_id):
                errors[CONF_ECU_ID] = "invalid_ecu_id"
            if not port:
                errors[CONF_PORT] = "invalid_port"
            if not errors:
                await self.async_set_unique_id(f"{DOMAIN}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"APS Zigbee ({port})",
                    data={
                        CONF_PORT: port,
                        CONF_ECU_ID: ecu_id,
                        CONF_INVERTERS: [],
                        CONF_DTR_RESET: dtr_reset,
                    },
                    options={CONF_SCAN_INTERVAL: scan_interval},
                )

        schema = await self._build_user_schema()
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _build_user_schema(self) -> vol.Schema:
        ports = await self.hass.async_add_executor_job(_list_serial_ports)
        port_selector: selector.Selector = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=ports,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            if ports
            else selector.TextSelector()
        )
        return vol.Schema(
            {
                vol.Required(CONF_PORT): port_selector,
                vol.Required(CONF_ECU_ID, default=DEFAULT_ECU_ID): selector.TextSelector(),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_S,
                        max=MAX_SCAN_INTERVAL_S,
                        step=10,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_DTR_RESET, default=DEFAULT_DTR_RESET
                ): selector.BooleanSelector(),
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return APSOptionsFlow(entry)


class APSOptionsFlow(OptionsFlow):
    """Manage inverters and polling interval after initial setup."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_inverter", "remove_inverter", "settings"],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_S,
                        max=MAX_SCAN_INTERVAL_S,
                        step=10,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_add_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            serial = user_input[INV_SERIAL].strip()
            name = (user_input.get(CONF_NAME) or "").strip()
            if not _SERIAL_RE.match(serial):
                errors[INV_SERIAL] = "invalid_serial"
            elif any(i[INV_SERIAL] == serial for i in self.entry.data.get(CONF_INVERTERS, [])):
                errors[INV_SERIAL] = "already_paired"
            else:
                coordinator = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
                if coordinator is None:
                    errors["base"] = "not_loaded"
                else:
                    try:
                        await coordinator.async_pair_new_inverter(
                            serial, name or f"APS DS3 {serial[-4:]}"
                        )
                    except PairingError:
                        errors["base"] = "pairing_failed"
                    else:
                        return self.async_create_entry(title="", data=self.entry.options)
        schema = vol.Schema(
            {
                vol.Required(INV_SERIAL): selector.TextSelector(),
                vol.Optional(CONF_NAME, default=""): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="add_inverter", data_schema=schema, errors=errors)

    async def async_step_remove_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        inverters = self.entry.data.get(CONF_INVERTERS, [])
        if not inverters:
            return self.async_abort(reason="no_inverters")
        if user_input is not None:
            serial = user_input[INV_SERIAL]
            coordinator = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
            if coordinator is not None:
                await coordinator.async_remove_inverter(serial)
            return self.async_create_entry(title="", data=self.entry.options)
        options = [
            selector.SelectOptionDict(
                value=inv[INV_SERIAL],
                label=f"{inv.get(INV_NAME) or inv[INV_SERIAL]} ({inv[INV_ID]})",
            )
            for inv in inverters
        ]
        schema = vol.Schema(
            {
                vol.Required(INV_SERIAL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.LIST
                    )
                )
            }
        )
        return self.async_show_form(step_id="remove_inverter", data_schema=schema)


def _list_serial_ports() -> list[selector.SelectOptionDict]:
    """Enumerate plausible USB serial ports for the CC2530 dongle.

    We list pyserial's `comports()` (no HA dep), preferring the `by-id`
    symlink when one exists — it survives udev renumbering, which matters
    in Docker setups where /dev/ttyUSB0 is volatile.
    """
    from serial.tools import list_ports  # type: ignore[import-untyped]

    options: list[selector.SelectOptionDict] = []
    for port in list_ports.comports():
        path = _prefer_by_id(port.device)
        label = port.description or port.device
        options.append(selector.SelectOptionDict(value=path, label=f"{label} ({path})"))
    return options


def _prefer_by_id(device: str) -> str:
    """Return the `/dev/serial/by-id/...` symlink for `device` if available."""
    from pathlib import Path

    by_id_dir = Path("/dev/serial/by-id")
    if not by_id_dir.is_dir():
        return device
    target = Path(device).resolve()
    for link in by_id_dir.iterdir():
        try:
            if link.resolve() == target:
                return str(link)
        except OSError:  # pragma: no cover
            continue
    return device
