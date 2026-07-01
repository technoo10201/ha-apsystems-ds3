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
from .aps_protocol.frames import (
    build_no_command,
    build_zdo_mgmt_lqi_request,
    build_zdo_mgmt_rtg_request,
)
from .aps_protocol.pairing import (
    PairingError,
    _build_invid_blacklist,
    pair_inverter,
)
from .aps_protocol.polling import (
    PollError,
    SerialMismatchError,
    poll_inverter,
    reboot_inverter,
)
from .aps_protocol.runtime import (
    InverterRuntime,
    InverterState,
    reset_night_counters,
)
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
    RECOVERY_IDLE_RETRY_S,
    RECOVERY_RETRIES,
    SENSOR_ACV,
    SENSOR_ENERGY_P1,
    SENSOR_ENERGY_P2,
    SENSOR_FREQUENCY,
    SENSOR_IDC1,
    SENSOR_IDC2,
    SENSOR_MESH_HOPS,
    SENSOR_POWER_P1,
    SENSOR_POWER_P2,
    SENSOR_POWER_TOTAL,
    SENSOR_SIGNAL_QUALITY,
    SENSOR_TEMPERATURE,
    SENSOR_VDC1,
    SENSOR_VDC2,
    SERVICE_CANCEL_REBIND,
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
        # Re-entrancy guard for _async_recover: the watchdog and the polling
        # loop can both request a recovery in the same window; only one may
        # drive the serial port at a time. Also remembers when the last
        # (exhausted) recovery ended so the watchdog can retry at a slow,
        # steady cadence instead of giving up forever.
        self._recovering = False
        self._last_recovery_end: datetime | None = None
        # Tracks the day/night state of the previous update cycle so the
        # night→day transition (sunrise) can reset the failure counters.
        # Starting at False means the first daytime cycle after a (re)start
        # triggers a reset on already-zeroed counters — harmless.
        self._was_sun_up = False
        # Persistent re-bind campaign (rebind_persistent service) and the
        # "a pair handshake is on the air right now" flag. While the flag is
        # set the coordinator is in no-NO mode and unicast polls would all
        # fail with INVALID_PARAMETER — the polling loop skips the bus
        # entirely instead of recording bogus failures.
        self._campaign_task: asyncio.Task[None] | None = None
        self._pairing_active = False

    # ------------------------------------------------------------------ lifecycle

    async def async_open(self) -> None:
        """Open the serial port and bring up the CC2530 coordinator."""
        await self.znp.open()
        if not await init_coordinator(self.znp, self._ecu_id):
            await self.znp.close()
            raise ConfigEntryNotReady(f"CC2530 dongle on {self.znp.port} did not come up")
        self._init_done = True

    async def async_close(self) -> None:
        """Stop watchdog + campaign and close the underlying serial transport."""
        await self.async_cancel_rebind_campaign()
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
        if self._pairing_active:
            _LOGGER.info(
                "refresh of %s skipped: a pair handshake is in progress",
                serial,
            )
            return
        runtime = self._runtime(serial)
        now = _utcnow()
        data: dict[str, dict[str, Any]] = dict(self.data or {})
        try:
            result = await poll_inverter(
                self.znp, inv[INV_ID], self._ecu_id, expected_serial=serial
            )
        except (PollError, ZNPError) as err:
            _LOGGER.warning("on-demand poll failed for %s: %s", serial, err)
            runtime.record_failure(
                now,
                sun_is_up=is_up(self.hass) and not self._campaign_running,
                dead_threshold=DEAD_THRESHOLD,
                base_s=BACKOFF_BASE_S,
                cap_s=BACKOFF_MAX_S,
            )
            data[serial] = self._render_failure(serial, runtime)
        else:
            reading = result.reading
            runtime.record_success(now)
            prev = self._previous.get(serial)
            p1, p2, ptot = derive_power(prev, reading)
            self._previous[serial] = reading
            data[serial] = self._render_success(
                serial, reading, runtime, p1, p2, ptot, result.relays
            )
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

    async def async_discover(self) -> dict[str, list[str]]:
        """Dump the dongle's neighbour + routing tables to the HA log.

        Diagnostic helper: when a configured inverter never answers polls
        (timeout, no AF_DATA_CONFIRM) and a Re-bind also fails to extract
        a fresh short address, we don't know whether the inverter is
        offline, on the mesh under a different short address, or has lost
        its ECU binding entirely. This method asks the CC2530 itself for
        its view of the network via two standard ZDO management
        commands:

        - ``ZDO_MGMT_LQI_REQ`` (cmd 0x2531) → direct neighbour table
          (short_addr, ext_addr, depth, LQI for each device the dongle
          hears directly).
        - ``ZDO_MGMT_RTG_REQ`` (cmd 0x2532) → routing table
          (destination short_addr + next-hop short_addr for every route
          the dongle has discovered, including multi-hop mesh routes).

        Returns a dict ``{"neighbours": [...], "routes": [...]}`` where
        each entry is a short, human-readable string. The same data is
        logged at INFO level so it shows up in the standard HA log even
        without the integration in debug mode.
        """
        if not self._init_done:
            await self.async_open()

        neighbour_raw = await self.znp.request(
            build_zdo_mgmt_lqi_request(dst_addr="0000", start_index=0)
        )
        route_raw = await self.znp.request(
            build_zdo_mgmt_rtg_request(dst_addr="0000", start_index=0)
        )

        neighbours = _parse_zdo_mgmt_lqi_response(neighbour_raw)
        routes = _parse_zdo_mgmt_rtg_response(route_raw)

        _LOGGER.info(
            "APS Zigbee discover — neighbours (%d):\n  %s",
            len(neighbours),
            "\n  ".join(neighbours) if neighbours else "(none)",
        )
        _LOGGER.info(
            "APS Zigbee discover — routes (%d):\n  %s",
            len(routes),
            "\n  ".join(routes) if routes else "(none)",
        )
        return {"neighbours": neighbours, "routes": routes}

    async def async_rebind_inverter(self, serial: str) -> str:
        """Re-run the 4-frame pair handshake against an already-known inverter.

        Use case: the inverter was added via the manual-invID path (no
        handshake at registration time), or the dongle's NWK table got
        wiped (SYS_RESET on first startup after dongle was moved to a new
        host). In both cases the CC2530 doesn't have a route to the
        inverter's short address, and unicast polls fail with timeout or
        `AF_DATA_CONFIRM` status 0xCD (ZNwkNoRoute).

        The pair handshake uses `DstAddr=FFFF` (broadcast) at the MAC
        layer, so it propagates through the Zigbee mesh — the inverter
        only needs to be reachable through *any* mesh hop, not directly
        from the dongle. Its response carries its short address and
        teaches the CC2530 the route automatically.

        Returns the existing inv_id (preserved from the config entry).
        Raises `PairingError` if the handshake doesn't get an answer.
        """
        inverter = next(
            (
                i
                for i in self.entry.data.get(CONF_INVERTERS, [])
                if i[INV_SERIAL] == serial
            ),
            None,
        )
        if inverter is None:
            raise PairingError(f"unknown inverter serial {serial!r}")
        expected_inv_id = inverter[INV_ID]

        if not self._init_done:
            await self.async_open()
        # While the handshake is on the air the coordinator is in no-NO mode:
        # flag it so the regular polling loop skips the bus instead of
        # recording bogus failures for every healthy inverter.
        self._pairing_active = True
        try:
            if not await init_coordinator(
                self.znp, self._ecu_id, normal_ops=False
            ):
                raise PairingError("coordinator re-init failed before re-bind")
            # Exclude all paired short addresses except this one's, so the
            # extractor is allowed to return `expected_inv_id` if the inverter
            # echoes its known address.
            other_inv_ids = [
                i[INV_ID]
                for i in self.entry.data.get(CONF_INVERTERS, [])
                if i[INV_SERIAL] != serial
            ]
            try:
                observed_inv_id = await pair_inverter(
                    self.znp, serial, self._ecu_id, known_inv_ids=other_inv_ids
                )
            finally:
                try:
                    await self.znp.request(build_no_command(self._ecu_id))
                except ZNPError:
                    _LOGGER.debug(
                        "post-rebind sendNO failed (will retry next cycle)"
                    )
        finally:
            self._pairing_active = False

        await asyncio.sleep(2.0)

        if observed_inv_id and observed_inv_id != expected_inv_id:
            _LOGGER.warning(
                "re-bind for %s returned inv_id %s but config has %s; "
                "the inverter may have been re-paired against another ECU. "
                "Updating the config entry to match the observed value.",
                serial,
                observed_inv_id,
                expected_inv_id,
            )
            new_inverters = [
                {**i, INV_ID: observed_inv_id} if i[INV_SERIAL] == serial else i
                for i in self.entry.data.get(CONF_INVERTERS, [])
            ]
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_INVERTERS: new_inverters},
            )

        await self.async_request_refresh()
        return observed_inv_id or expected_inv_id

    @property
    def _campaign_running(self) -> bool:
        """True while a persistent re-bind campaign task is alive."""
        return self._campaign_task is not None and not self._campaign_task.done()

    async def async_start_rebind_campaign(
        self, serial: str, duration_min: int, pause_s: int
    ) -> None:
        """Start a persistent re-bind campaign for one inverter.

        A marginal radio link fluctuates (PV output, weather, multipath):
        a handshake that fails now can succeed an hour later. The campaign
        re-runs the short re-bind cycle every `pause_s` seconds for up to
        `duration_min` minutes, sampling the channel until a lucky window —
        the honest software-only attempt before physically moving the
        dongle next to the inverter.

        Each cycle is the existing `async_rebind_inverter` (~20-25 s, with
        sendNO restored in its finally), so normal polling of the other
        inverters resumes between cycles and the watchdog never starves.
        """
        if self._campaign_task is not None and not self._campaign_task.done():
            raise PairingError(
                "a re-bind campaign is already running — cancel it first "
                f"({SERVICE_CANCEL_REBIND})"
            )
        if not any(
            i[INV_SERIAL] == serial
            for i in self.entry.data.get(CONF_INVERTERS, [])
        ):
            raise PairingError(f"unknown inverter serial {serial!r}")
        self._campaign_task = self.hass.loop.create_task(
            self._rebind_campaign_loop(serial, duration_min, pause_s)
        )

    async def async_cancel_rebind_campaign(self) -> None:
        """Stop the running re-bind campaign, if any (idempotent)."""
        task = self._campaign_task
        if task is None or task.done():
            self._campaign_task = None
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._campaign_task = None
        self._pairing_active = False
        _LOGGER.info("re-bind campaign cancelled")

    async def _rebind_campaign_loop(
        self, serial: str, duration_min: int, pause_s: int
    ) -> None:
        deadline = _utcnow() + timedelta(minutes=duration_min)
        cycle = 0
        _LOGGER.warning(
            "re-bind campaign started for %s: up to %d min, one attempt "
            "every %d s",
            serial,
            duration_min,
            pause_s,
        )
        try:
            while _utcnow() < deadline:
                cycle += 1
                try:
                    inv_id = await self.async_rebind_inverter(serial)
                except (PairingError, ZNPError) as err:
                    remaining = int((deadline - _utcnow()).total_seconds() / 60)
                    _LOGGER.info(
                        "re-bind campaign for %s: cycle %d failed (%s) — "
                        "~%d min left",
                        serial,
                        cycle,
                        err,
                        remaining,
                    )
                else:
                    _LOGGER.warning(
                        "re-bind campaign for %s SUCCEEDED on cycle %d "
                        "(inv_id %s)",
                        serial,
                        cycle,
                        inv_id,
                    )
                    self._notify(
                        "Ré-association réussie",
                        f"L'onduleur {serial} a répondu au cycle {cycle} "
                        f"(adresse {inv_id}). Le polling reprend "
                        "automatiquement.",
                    )
                    return
                await asyncio.sleep(pause_s)
            _LOGGER.warning(
                "re-bind campaign for %s expired after %d cycles without an "
                "answer",
                serial,
                cycle,
            )
            self._notify(
                "Ré-association échouée",
                f"L'onduleur {serial} n'a pas répondu en {duration_min} min "
                f"({cycle} tentatives). Réessayez en pleine production "
                "(midi solaire) ou refaites l'appairage avec le dongle à "
                "proximité de l'onduleur.",
            )
        finally:
            self._pairing_active = False
            self._campaign_task = None

    def _notify(self, title: str, message: str) -> None:
        """Fire-and-forget persistent notification (best effort)."""
        try:
            from homeassistant.components.persistent_notification import (
                async_create,
            )

            async_create(self.hass, message, title=title)
        except Exception:  # pragma: no cover - notification is cosmetic
            _LOGGER.debug("persistent notification failed", exc_info=True)

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

        if self._pairing_active:
            # A pair handshake is on the air: the coordinator is in no-NO
            # mode and every unicast poll would fail with INVALID_PARAMETER.
            # Serve the previous values untouched instead of recording
            # bogus failures against healthy inverters.
            return {
                inv[INV_SERIAL]: self._carry_forward(
                    inv[INV_SERIAL], self._runtime(inv[INV_SERIAL])
                )
                for inv in self.inverters
            }

        sun_is_up = is_up(self.hass)
        if sun_is_up and not self._was_sun_up:
            # Sunrise: inverters wake up gradually with the light, so any
            # failure streak carried over from the previous evening must not
            # shorten their morning grace period (README "fresh five chances").
            reset_night_counters(self._runtimes)
            _LOGGER.debug("sunrise detected — failure counters reset")
        self._was_sun_up = sun_is_up

        # Every re-bind cycle SYS_RESETs the dongle, wiping its mesh routing
        # tables; multi-hop inverters legitimately fail to answer until the
        # routes rebuild. While a campaign is running those failures are
        # campaign-induced, not inverter faults — gate them to IDLE (same
        # mechanism as night-time) so healthy inverters never get escalated
        # to DEAD by our own radio activity.
        count_failures = sun_is_up and not self._campaign_running

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
                poll_result = await poll_inverter(
                    self.znp, inv_id, self._ecu_id, expected_serial=serial
                )
            except (PollError, ZNPError) as err:
                # A serial mismatch is a configuration error (swapped invIDs),
                # not a radio hiccup — surface it loudly instead of at debug.
                log = (
                    _LOGGER.warning
                    if isinstance(err, SerialMismatchError)
                    else _LOGGER.debug
                )
                log("poll failed for %s (%s): %s", serial, inv_id, err)
                runtime.record_failure(
                    now,
                    sun_is_up=count_failures,
                    dead_threshold=DEAD_THRESHOLD,
                    base_s=BACKOFF_BASE_S,
                    cap_s=BACKOFF_MAX_S,
                )
                result[serial] = self._render_failure(serial, runtime)
                continue

            all_failed = False
            reading = poll_result.reading
            runtime.record_success(now)
            prev = self._previous.get(serial)
            p1, p2, ptot = derive_power(prev, reading)
            self._previous[serial] = reading
            result[serial] = self._render_success(
                serial, reading, runtime, p1, p2, ptot, poll_result.relays
            )

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
                    # A previous recovery exhausted its retries (or one is on
                    # the air right now). Never give up for good: the CH340 is
                    # known to drop off the bus and come back, so retry at a
                    # slow, steady cadence instead of leaving the integration
                    # dead until a human reloads it.
                    if (
                        not self._recovering
                        and (
                            self._last_recovery_end is None
                            or (_utcnow() - self._last_recovery_end)
                            >= timedelta(seconds=RECOVERY_IDLE_RETRY_S)
                        )
                    ):
                        _LOGGER.warning(
                            "watchdog: coordinator still down, retrying recovery"
                        )
                        await self._async_recover()
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

        Re-entrant calls (watchdog + polling loop racing) coalesce into the
        recovery already in flight instead of driving the port concurrently.
        """
        if self._recovering:
            _LOGGER.debug("recovery already in progress, skipping duplicate request")
            return
        self._recovering = True
        try:
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
            _LOGGER.error(
                "coordinator recovery exhausted; sensors unavailable, retrying in %s s",
                RECOVERY_IDLE_RETRY_S,
            )
        finally:
            self._recovering = False
            self._last_recovery_end = _utcnow()

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
        serial: str,
        reading: DS3Reading,
        runtime: InverterRuntime,
        p1: float,
        p2: float,
        ptot: float,
        relays: list[str] | None,
    ) -> dict[str, Any]:
        payload = _build_reading_dict(reading, p1, p2, ptot)
        payload.update(runtime.to_attributes())
        payload["available"] = True
        if relays is None:
            # The firmware doesn't announce the route on every poll — keep
            # the last known value instead of flapping to unknown.
            last = (self.data or {}).get(serial) or {}
            payload[SENSOR_MESH_HOPS] = last.get(SENSOR_MESH_HOPS)
            payload["route"] = last.get("route")
        else:
            payload[SENSOR_MESH_HOPS] = len(relays)
            payload["route"] = [self._inverter_label(addr) for addr in relays]
        return payload

    def _inverter_label(self, inv_id: str) -> str:
        """Best-effort translation of a relay short address to a friendly name."""
        for inv in self.inverters:
            if inv[INV_ID].upper() == inv_id.upper():
                return inv.get(INV_NAME) or inv[INV_SERIAL]
        return inv_id

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


def _iter_4xxx_payloads(burst_hex: str, cmd0: str, cmd1: str) -> list[str]:
    """Return every `(cmd0, cmd1)` frame's payload hex from a raw ZNP burst.

    Used to pick the `4531` and `4532` AREQ responses out of the burst that
    SAPI emits in reply to a `2531` / `2532` request (which include the SRSP
    + the actual AREQ, sometimes back-to-back).
    """
    out: list[str] = []
    burst = burst_hex.upper()
    target_cmd0 = cmd0.upper()
    target_cmd1 = cmd1.upper()
    i = 0
    while i < len(burst):
        if burst[i : i + 2] != "FE":
            i += 2
            continue
        if i + 10 > len(burst):
            break
        try:
            length = int(burst[i + 2 : i + 4], 16)
        except ValueError:
            i += 2
            continue
        payload_start = i + 8
        payload_end = payload_start + 2 * length
        if payload_end + 2 > len(burst):
            break
        if (
            burst[i + 4 : i + 6] == target_cmd0
            and burst[i + 6 : i + 8] == target_cmd1
        ):
            out.append(burst[payload_start:payload_end])
        i = payload_end + 2  # skip the FCS byte
    return out


def _parse_zdo_mgmt_lqi_response(burst_hex: str) -> list[str]:
    """Decode `ZDO_MGMT_LQI_RSP` (cmd 0x4531) entries from a raw ZNP burst.

    Response payload layout (per TI Z-Stack ZNP API):
        SrcAddr (2 LE)
        Status (1)
        NeighborTableEntries (1)
        StartIndex (1)
        NeighborLqiListCount (1)
        N × entry of 22 bytes each:
            ExtPanId (8)  ExtAddr (8)  NetworkAddr (2 LE)
            TypeBytes (1) PermitJoining (1) Depth (1) LQI (1)

    We surface only the fields useful to a human reader: the short address
    in BE form and the LQI; plus the device type (`coordinator`, `router`,
    `end_device`) extracted from the TypeBytes nibble.
    """
    entries: list[str] = []
    for payload in _iter_4xxx_payloads(burst_hex, "45", "31"):
        if len(payload) < 14:
            continue
        list_count = int(payload[12:14], 16)
        cursor = 14
        for _ in range(list_count):
            if cursor + 44 > len(payload):
                break
            entry = payload[cursor : cursor + 44]
            short_le = entry[32:36]
            short_be = (short_le[2:4] + short_le[0:2]).upper()
            type_byte = int(entry[36:38], 16)
            dev_type = {0: "coordinator", 1: "router", 2: "end_device"}.get(
                type_byte & 0x03, f"type_{type_byte & 0x03}"
            )
            depth = int(entry[40:42], 16)
            lqi = int(entry[42:44], 16)
            entries.append(
                f"short={short_be} type={dev_type} depth={depth} lqi={lqi}"
            )
            cursor += 44
    return entries


def _parse_zdo_mgmt_rtg_response(burst_hex: str) -> list[str]:
    """Decode `ZDO_MGMT_RTG_RSP` (cmd 0x4532) entries from a raw ZNP burst.

    Response payload layout (per TI Z-Stack ZNP API):
        SrcAddr (2 LE)
        Status (1)
        RoutingTableEntries (1)
        StartIndex (1)
        RoutingTableListCount (1)
        N × entry of 5 bytes each:
            DstAddr (2 LE)  StatusByte (1)  NextHop (2 LE)

    Status byte low 3 bits = route state (active, discovery underway,
    discovery failed, inactive, validation underway). The route state is
    the key signal for distinguishing "we know how to reach X" vs
    "discovery for X is in progress / failed".
    """
    states = {
        0: "active",
        1: "discovery_underway",
        2: "discovery_failed",
        3: "inactive",
        4: "validation_underway",
    }
    entries: list[str] = []
    for payload in _iter_4xxx_payloads(burst_hex, "45", "32"):
        if len(payload) < 14:
            continue
        list_count = int(payload[12:14], 16)
        cursor = 14
        for _ in range(list_count):
            if cursor + 10 > len(payload):
                break
            entry = payload[cursor : cursor + 10]
            dst_le = entry[0:4]
            dst_be = (dst_le[2:4] + dst_le[0:2]).upper()
            status_byte = int(entry[4:6], 16)
            state = states.get(status_byte & 0x07, f"state_{status_byte & 0x07}")
            next_le = entry[6:10]
            next_be = (next_le[2:4] + next_le[0:2]).upper()
            entries.append(f"dst={dst_be} state={state} next_hop={next_be}")
            cursor += 10
    return entries
