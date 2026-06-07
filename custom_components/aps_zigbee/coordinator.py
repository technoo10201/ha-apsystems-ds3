"""Home Assistant `DataUpdateCoordinator` for the APS DS3 integration.

Wraps the proprietary `aps_protocol` stack with:
  * a per-inverter state machine (`aps_protocol.runtime.InverterRuntime`) that
    handles exponential backoff and the day/night gating;
  * a watchdog task that pings the CC2530 every WATCHDOG_INTERVAL_S and
    triggers a full coordinator recovery if it doesn't answer;
  * on-demand single-inverter polling for the Refresh button on each device.

The data dict published to Home Assistant has the shape::

    {
        "<inverter_serial>": {
            "available": bool,            # mirrors state != dead
            "state": "ok" | "stale" | "idle" | "dead",
            "last_seen": ISO8601 | None,
            "consecutive_failures": int,
            SENSOR_VDC1: float, ...  # set on successful polls only
        },
        ...
    }
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.sun import is_up
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aps_protocol.coordinator import check_coordinator, init_coordinator
from .aps_protocol.decode_ds3 import DS3Reading, derive_power
from .aps_protocol.frames import build_no_command
from .aps_protocol.pairing import (
    PairingError,
    _build_invid_blacklist,
    pair_inverter,
)
from .aps_protocol.polling import PollError, poll_inverter, reboot_inverter
from .aps_protocol.runtime import InverterRuntime, InverterState
from .aps_protocol.znp import ZNP, ZNPError
from .const import (
    BACKOFF_BASE_S,
    BACKOFF_MAX_S,
    CONF_DTR_RESET,
    CONF_ECU_ID,
    CONF_INVERTERS,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEAD_THRESHOLD,
    DEFAULT_DTR_RESET,
    DEFAULT_SCAN_INTERVAL_S,
    DOMAIN,
    INV_ID,
    INV_NAME,
    INV_SERIAL,
    RECOVERY_BACKOFF_S,
    RECOVERY_RETRIES,
    SENSOR_ACV,
    SENSOR_ENERGY_P1,
    SENSOR_ENERGY_P2,
    SENSOR_FREQUENCY,
    SENSOR_IDC1,
    SENSOR_IDC2,
    SENSOR_POWER_P1,
    SENSOR_POWER_P2,
    SENSOR_POWER_TOTAL,
    SENSOR_SIGNAL_QUALITY,
    SENSOR_TEMPERATURE,
    SENSOR_VDC1,
    SENSOR_VDC2,
    WATCHDOG_INTERVAL_S,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class APSDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinates the single dongle that talks to every paired inverter."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.entry = entry
        self._ecu_id: str = entry.data[CONF_ECU_ID]
        self.znp = ZNP(
            entry.data[CONF_PORT],
            dtr_reset=entry.data.get(CONF_DTR_RESET, DEFAULT_DTR_RESET),
        )
        self._previous: dict[str, DS3Reading] = {}
        self._runtimes: dict[str, InverterRuntime] = {}
        self._init_done = False
        self._watchdog_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def async_open(self) -> None:
        """Open the serial port and bring up the CC2530 coordinator."""
        await self.znp.open()
        if not await init_coordinator(self.znp, self._ecu_id):
            await self.znp.close()
            raise ConfigEntryNotReady(f"CC2530 dongle on {self.znp.port} did not come up")
        self._init_done = True

    async def async_close(self) -> None:
        """Stop the watchdog and close the underlying serial transport."""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        await self.znp.close()
        self._init_done = False

    def start_watchdog(self) -> None:
        """Schedule the periodic coordinator liveness check.

        Idempotent — called from `__init__.py:async_setup_entry` after the
        first refresh succeeds.
        """
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = self.hass.loop.create_task(self._watchdog_loop())

    # ------------------------------------------------------------------ public

    @property
    def inverters(self) -> list[dict[str, str]]:
        """Return the list of paired inverters from the config entry."""
        return list(self.entry.data.get(CONF_INVERTERS, []))

    async def async_repoll_inverter(self, serial: str) -> None:
        """Force an immediate refresh of one inverter (Refresh button)."""
        inv = next((i for i in self.inverters if i[INV_SERIAL] == serial), None)
        if inv is None:
            raise KeyError(f"unknown inverter {serial}")
        runtime = self._runtime(serial)
        now = _utcnow()
        data: dict[str, dict[str, Any]] = dict(self.data or {})
        try:
            reading = await poll_inverter(self.znp, inv[INV_ID], self._ecu_id)
        except (PollError, ZNPError) as err:
            _LOGGER.warning("on-demand poll failed for %s: %s", serial, err)
            runtime.record_failure(
                now,
                sun_is_up=is_up(self.hass),
                dead_threshold=DEAD_THRESHOLD,
                base_s=BACKOFF_BASE_S,
                cap_s=BACKOFF_MAX_S,
            )
            data[serial] = self._render_failure(serial, runtime)
        else:
            runtime.record_success(now)
            prev = self._previous.get(serial)
            p1, p2, ptot = derive_power(prev, reading)
            self._previous[serial] = reading
            data[serial] = self._render_success(reading, runtime, p1, p2, ptot)
        self.async_set_updated_data(data)

    async def async_pair_new_inverter(self, serial: str, name: str) -> str:
        """Pair an inverter and persist it in the config entry.

        The dongle's firmware refuses AF_DATA_REQUEST_EXT pair frames (status
        0x02 INVALID_PARAMETER on every SRSP) once it has been put into
        normal-operations mode by `sendNO`. The upstream firmware therefore
        re-initialises the coordinator **without** the NO frame right before
        each pairing pass, then restores normal ops afterwards — see
        `ZIGBEE_PAIR.ino:7-40` (`coordinator(false)` + post-pair `sendNO()`).
        We mirror that here.
        """
        if not self._init_done:
            await self.async_open()
        if any(
            i[INV_SERIAL] == serial for i in self.entry.data.get(CONF_INVERTERS, [])
        ):
            raise PairingError(f"inverter {serial} is already paired")

        # Re-init the coordinator without sendNO so the radio accepts EXT
        # data requests for the pairing handshake.
        if not await init_coordinator(self.znp, self._ecu_id, normal_ops=False):
            raise PairingError("coordinator re-init failed before pairing")
        # Pass already-paired short addresses so the extractor never returns
        # a neighbour's invID for the new inverter (see pairing._extract_inv_id).
        existing_inv_ids = [
            i[INV_ID] for i in self.entry.data.get(CONF_INVERTERS, [])
        ]
        try:
            inv_id = await pair_inverter(
                self.znp, serial, self._ecu_id, known_inv_ids=existing_inv_ids
            )
        finally:
            # Restore normal operations no matter what — otherwise polling
            # would fail because the radio is left in a half-initialised state.
            try:
                await self.znp.request(build_no_command(self._ecu_id))
            except ZNPError:
                _LOGGER.debug("post-pair sendNO failed (will retry next cycle)")

        # Give the asyncio serial transport time to settle after the pair burst
        # before the first poll fires. Without this the next znp.request() hits
        # "transport write failed: Connection lost" ~500 ms post-pair, which
        # triggers a SYS_RESET-based recovery that wipes the just-learned route
        # to the inverter. Mirrors test_local/main.py:52 (3 s) and
        # aps_yc600.py:486 (1 s).
        await asyncio.sleep(2.0)

        new_inverters = list(self.entry.data.get(CONF_INVERTERS, []))
        new_inverters.append({INV_SERIAL: serial, INV_ID: inv_id, INV_NAME: name})
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_INVERTERS: new_inverters}
        )
        await self.async_request_refresh()
        return inv_id

    async def async_register_inverter(
        self, serial: str, name: str, inv_id: str
    ) -> str:
        """Persist (serial, inv_id) without running the live pair handshake.

        Useful when the inverter is already firmware-bound to this ECU — e.g.
        previously paired on another host with the same dongle / same ECU id.
        The binding lives in the inverter's flash and survives the dongle
        move; the plugin just needs the (serial, inv_id) mapping to start
        polling. Skipping the handshake also avoids the antenna-proximity
        constraint that the regular pair flow needs (see the warning in the
        add_inverter description).

        Validation mirrors the dynamic blacklist `pair_inverter` uses on the
        extraction side: 4-hex-char format, not `0000`/`FFFF`, not the ECU's
        own short address, not already in use by another paired inverter.
        Raises `ValueError` if the inv_id is malformed or reserved.
        """
        inv_id = inv_id.strip().upper()
        if len(inv_id) != 4 or any(c not in "0123456789ABCDEF" for c in inv_id):
            raise ValueError(f"invalid inv_id {inv_id!r}; expected 4 hex chars")
        if any(
            i[INV_SERIAL] == serial
            for i in self.entry.data.get(CONF_INVERTERS, [])
        ):
            raise PairingError(f"inverter {serial} is already paired")
        existing_inv_ids = [
            i[INV_ID] for i in self.entry.data.get(CONF_INVERTERS, [])
        ]
        blacklist = _build_invid_blacklist(self._ecu_id, existing_inv_ids)
        if inv_id in blacklist:
            raise ValueError(
                f"inv_id {inv_id!r} is reserved (broadcast / ECU short addr / "
                "already in use)"
            )

        new_inverters = list(self.entry.data.get(CONF_INVERTERS, []))
        new_inverters.append({INV_SERIAL: serial, INV_ID: inv_id, INV_NAME: name})
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_INVERTERS: new_inverters}
        )
        await self.async_request_refresh()
        return inv_id

    async def async_remove_inverter(self, serial: str) -> None:
        """Forget an inverter from the config entry and HA's registries.

        Drops the inverter from the config entry's list and purges its
        device + entity rows from `device_registry` / `entity_registry`.
        Without the registry cleanup the device card and its 13 sensors
        keep showing in the UI as orphans after a Remove Inverter — they
        no longer poll but the user sees a stale "unknown"-valued card
        forever.
        """
        new_inverters = [
            i for i in self.entry.data.get(CONF_INVERTERS, []) if i[INV_SERIAL] != serial
        ]
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_INVERTERS: new_inverters}
        )
        self._previous.pop(serial, None)
        self._runtimes.pop(serial, None)

        device_reg = dr.async_get(self.hass)
        device = device_reg.async_get_device(identifiers={(DOMAIN, serial)})
        if device is not None:
            # async_remove_device cascades to the entity_registry, so we
            # don't have to walk the entities ourselves.
            device_reg.async_remove_device(device.id)

        await self.async_request_refresh()

    async def async_reboot_inverter(self, serial: str) -> None:
        """Send the proprietary reboot command to an inverter."""
        inv = next((i for i in self.inverters if i[INV_SERIAL] == serial), None)
        if inv is None:
            raise KeyError(f"unknown inverter {serial}")
        await reboot_inverter(self.znp, inv[INV_ID], self._ecu_id)

    # ------------------------------------------------------------------ update loop

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        if not self._init_done:
            raise UpdateFailed("coordinator not initialised")

        sun_is_up = is_up(self.hass)
        now = _utcnow()
        result: dict[str, dict[str, Any]] = {}
        all_failed = True

        for inv in self.inverters:
            serial = inv[INV_SERIAL]
            inv_id = inv[INV_ID]
            runtime = self._runtime(serial)

            if runtime.should_skip(now):
                # We are still in the backoff window — keep the previous data
                # (if any) and don't talk to the bus.
                result[serial] = self._carry_forward(serial, runtime)
                continue

            try:
                reading = await poll_inverter(self.znp, inv_id, self._ecu_id)
            except (PollError, ZNPError) as err:
                _LOGGER.debug("poll failed for %s (%s): %s", serial, inv_id, err)
                runtime.record_failure(
                    now,
                    sun_is_up=sun_is_up,
                    dead_threshold=DEAD_THRESHOLD,
                    base_s=BACKOFF_BASE_S,
                    cap_s=BACKOFF_MAX_S,
                )
                result[serial] = self._render_failure(serial, runtime)
                continue

            all_failed = False
            runtime.record_success(now)
            prev = self._previous.get(serial)
            p1, p2, ptot = derive_power(prev, reading)
            self._previous[serial] = reading
            result[serial] = self._render_success(reading, runtime, p1, p2, ptot)

        # If every inverter failed and we have at least one paired, we very
        # likely have a coordinator problem rather than N independent inverter
        # problems — schedule a recovery on the next watchdog tick.
        if self.inverters and all_failed and sun_is_up and not self.znp.is_open:
            self.hass.loop.create_task(self._async_recover())

        return result

    # ------------------------------------------------------------------ watchdog + recovery

    async def _watchdog_loop(self) -> None:
        """Poke the coordinator periodically; trigger recovery on silence."""
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                if not self._init_done:
                    continue
                # Skip if a recent polling cycle already succeeded — it counts
                # as a liveness proof and we don't want to add load.
                if (
                    self.last_update_success
                    and self.last_update_success_time is not None
                    and (_utcnow() - self.last_update_success_time)
                    < timedelta(seconds=WATCHDOG_INTERVAL_S)
                ):
                    continue
                try:
                    alive = await check_coordinator(self.znp)
                except ZNPError as err:
                    _LOGGER.warning("watchdog: transport error: %s", err)
                    alive = False
                if not alive:
                    _LOGGER.warning("watchdog: coordinator did not answer, recovering")
                    await self._async_recover()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("watchdog crashed; restarting in 60 s")
            await asyncio.sleep(60)
            self.start_watchdog()

    async def _async_recover(self) -> None:
        """Close + reopen + (soft- or hard-) re-init the coordinator with backoff.

        Two paths:
          - **Soft**: the dongle still answers `check_coordinator` after the
            reopen → trust that the NWK table (and paired inverters' routes)
            survived. Skip the full init so we don't SYS_RESET those routes
            away.
          - **Hard**: dongle is wedged → fall back to `init_coordinator`, which
            does a SYS_RESET. Already-paired inverters will need to be
            re-paired by the user because their short-addr routes are gone.
        """
        await self.znp.close()
        self._init_done = False
        for attempt in range(RECOVERY_RETRIES):
            delay = RECOVERY_BACKOFF_S[min(attempt, len(RECOVERY_BACKOFF_S) - 1)]
            _LOGGER.info("coordinator recovery attempt %s after %s s", attempt + 1, delay)
            await asyncio.sleep(delay)
            try:
                await self.znp.open()
                if await check_coordinator(self.znp):
                    self._init_done = True
                    _LOGGER.info("coordinator soft-recovered on attempt %s", attempt + 1)
                    await self.async_request_refresh()
                    return
                if await init_coordinator(self.znp, self._ecu_id):
                    self._init_done = True
                    _LOGGER.info("coordinator hard-recovered on attempt %s", attempt + 1)
                    await self.async_request_refresh()
                    return
            except ZNPError as err:
                _LOGGER.warning("recovery attempt %s failed: %s", attempt + 1, err)
                await self.znp.close()
        _LOGGER.error("coordinator recovery exhausted; sensors will go unavailable")

    # ------------------------------------------------------------------ helpers

    def _runtime(self, serial: str) -> InverterRuntime:
        rt = self._runtimes.get(serial)
        if rt is None:
            rt = InverterRuntime(serial=serial)
            self._runtimes[serial] = rt
        return rt

    @property
    def last_update_success_time(self) -> datetime | None:
        """Best-effort accessor for HA's recorded last success timestamp."""
        # `DataUpdateCoordinator` exposes `last_update_success` (bool) and
        # `last_update` (datetime of last attempt). Use the latter when the
        # last attempt succeeded.
        if not self.last_update_success:
            return None
        ts = getattr(self, "last_update", None)
        if isinstance(ts, datetime):
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return None

    def _render_success(
        self,
        reading: DS3Reading,
        runtime: InverterRuntime,
        p1: float,
        p2: float,
        ptot: float,
    ) -> dict[str, Any]:
        payload = _build_reading_dict(reading, p1, p2, ptot)
        payload.update(runtime.to_attributes())
        payload["available"] = True
        return payload

    def _render_failure(self, serial: str, runtime: InverterRuntime) -> dict[str, Any]:
        last = (self.data or {}).get(serial) or {}
        payload = dict(last)
        payload.update(runtime.to_attributes())
        payload["available"] = runtime.state is not InverterState.DEAD
        return payload

    def _carry_forward(self, serial: str, runtime: InverterRuntime) -> dict[str, Any]:
        last = (self.data or {}).get(serial) or {}
        payload = dict(last)
        payload.update(runtime.to_attributes())
        # availability follows the existing state — backoff doesn't flip it.
        payload.setdefault("available", runtime.state is not InverterState.DEAD)
        return payload


def _build_reading_dict(reading: DS3Reading, p1: float, p2: float, ptot: float) -> dict[str, Any]:
    """Map a `DS3Reading` + derived powers onto the sensor-key contract."""
    return {
        SENSOR_VDC1: reading.vdc1_v,
        SENSOR_VDC2: reading.vdc2_v,
        SENSOR_IDC1: reading.idc1_a,
        SENSOR_IDC2: reading.idc2_a,
        SENSOR_POWER_P1: p1,
        SENSOR_POWER_P2: p2,
        SENSOR_POWER_TOTAL: ptot,
        SENSOR_ENERGY_P1: reading.energy_p1_wh / 1000.0,
        SENSOR_ENERGY_P2: reading.energy_p2_wh / 1000.0,
        SENSOR_ACV: reading.acv_v,
        SENSOR_FREQUENCY: reading.freq_hz,
        SENSOR_TEMPERATURE: reading.temperature_c,
        SENSOR_SIGNAL_QUALITY: reading.signal_quality_pct,
    }


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
