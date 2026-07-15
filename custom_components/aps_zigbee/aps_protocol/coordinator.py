"""CC2530 coordinator bring-up sequence for the APsystems firmware.

The dongle needs the 8 init commands from `coordinator_init()`
(`ZIGBEE_COORDINATOR.ino:44-138`) plus the "normal operations" frame from
`sendNO()` (`:144-177`) before it can either pair or poll inverters.

We can't pulse the CC2530 RESET pin from a USB dongle running inside a Docker
container (no GPIO). The init sequence already includes a software reset
(`410000` aka `ZB_SYS_RESET_REQ`), so that's what we rely on. If the dongle
gets wedged, the user must physically re-plug it.
"""

from __future__ import annotations

import asyncio
import logging

from .frames import (
    CoordinatorDeviceInfo,
    build_check_alive_command,
    build_coordinator_init_commands,
    build_device_info_command,
    build_no_command,
    parse_device_info,
)
from .znp import ZNP, ZNPError

_LOGGER = logging.getLogger(__name__)

# Settling time after a SAPI_GET_DEVICE_INFO (0x6700) command.  The Kadsol
# firmware requires this pause before the next command can be queued safely,
# mirroring the same delay already present in `init_coordinator`.
_SETTLE_AFTER_DEVICE_INFO_S = 1.5

# Marker found in the dongle reply when a write-config / start-request command
# succeeded. Comes from the comments in `ZIGBEE_COORDINATOR.ino:50-66`.
_OK_WRITE_CONFIG = "66050062"  # FE0166050062 == "config write OK"
_OK_START = "64000065"  # FE0164000065 == "ZB_START_REQUEST OK"
_OK_RESET = "4180"  # FE064180...  == "ZB_SYS_RESET_REQ_IND"


async def init_coordinator(znp: ZNP, ecu_id: str, *, normal_ops: bool = True) -> bool:
    """Run the 9-step init then optionally the NO command.

    Returns True if the dongle is up. We don't try to interpret every reply —
    matching the firmware, we just look for one of the success markers in the
    burst that follows the start-request.

    The `aps_yc600.py` reference (the script the user already runs against
    his rig) sleeps 1.5 s after the last few init commands before moving on;
    we mirror that for the start-request (cmd index 7 `2600`) and the
    SAPI device info query (cmd index 8 `6700`). Without this settling time
    the dongle's radio doesn't fully come up and subsequent
    `AF_DATA_REQUEST_EXT` (pair) frames get rejected with status 0x02.
    """
    cmds = build_coordinator_init_commands(ecu_id)
    last_reply = ""
    for i, cmd in enumerate(cmds):
        try:
            reply = await znp.request(cmd)
        except ZNPError:
            _LOGGER.exception("coordinator init failed at step %s (%s)", i, cmd)
            return False
        _LOGGER.debug("coordinator init step %s: tx=%s rx=%s", i, cmd, reply)
        last_reply = reply
        # 2600 starts the radio; 6700 reads device info. Both need extra
        # settling time before the next command can be queued.
        if cmd in ("2600", "6700"):
            await asyncio.sleep(_SETTLE_AFTER_DEVICE_INFO_S)

    if normal_ops:
        try:
            await znp.request(build_no_command(ecu_id))
        except ZNPError:
            _LOGGER.exception("sendNO failed")
            return False

    return await check_coordinator(znp) or any(
        marker in last_reply for marker in (_OK_START, _OK_RESET, _OK_WRITE_CONFIG)
    )


async def check_coordinator(znp: ZNP) -> bool:
    """Return True if the dongle answers our liveness ping.

    We send `2700` (ZNP `SYS` subsystem ping); the firmware answers with a
    SRSP frame whose CMD0 byte is `0x67` (any other valid SRSP works too).
    In practice we treat *any* non-empty answer that contains the SOF byte
    `FE` as a successful liveness proof — the only failure mode that matters
    is "the dongle didn't answer at all".
    """
    try:
        reply = await znp.request(build_check_alive_command())
    except ZNPError:
        return False
    return bool(reply) and "FE" in reply.upper()


async def check_network(znp: ZNP) -> CoordinatorDeviceInfo | None:
    """Probe the Zigbee network state via SAPI_GET_DEVICE_INFO (0x6700).

    Returns a `CoordinatorDeviceInfo` describing whether the network is up
    (DeviceState=0x09, ShortAddr=0x0000) or stuck in DEV_HOLD after a serial
    re-enumeration.  Returns None on transport error (ZNPError) so the caller
    treats both "silent" and "wedged" as requiring hard recovery.

    After a successful request the function waits `_SETTLE_AFTER_DEVICE_INFO_S`
    seconds — the Kadsol firmware requires this settling time after any 6700
    query before the next command can be queued, mirroring the delay already
    present in `init_coordinator`.

    Criterion for soft recovery: `info is not None and info.network_up`.
    Merely answering on the serial line is not enough — the Zigbee network
    must be confirmed operational.
    """
    try:
        burst = await znp.request(build_device_info_command())
    except ZNPError:
        return None
    info = parse_device_info(burst)
    await asyncio.sleep(_SETTLE_AFTER_DEVICE_INFO_S)
    return info
