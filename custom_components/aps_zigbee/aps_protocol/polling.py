"""High-level "poll one inverter and decode its answer" pipeline.

This wraps `frames.build_poll_command`, the ZNP transport, the response
sanity checks taken from `AAA_DECODE.ino:34-64` and the stateless DS3 decoder.
"""

from __future__ import annotations

import logging

from .decode_ds3 import DS3DecodeError, DS3Reading, decode_ds3_frame
from .frames import build_poll_command, build_reboot_command
from .znp import ZNP, ZNPError

_LOGGER = logging.getLogger(__name__)

# Markers the firmware looks for in the burst to decide whether the polling
# round-trip went through. See the `fault = 10/11/13` branches in
# `AAA_DECODE.ino:34-53`.
_OK_AF_DATA_REQUEST = "FE01640100"
_OK_AF_DATA_CONFIRM = "FE03448000"
_AF_INCOMING_PREFIX = "4481"


class PollError(Exception):
    """The dongle did not return a decodable inverter frame."""


class SerialMismatchError(PollError):
    """The inverter that answered is not the one the config maps to this inv_id.

    Every AF_INCOMING_MSG carries the responding inverter's own serial
    (`decode_ds3.DS3Reading.serial`). When it differs from the serial the
    config entry associates with the polled short address, the (serial ↔
    inv_id) mapping is wrong — typically two inverters swapped during manual
    invID entry. Silently accepting the frame would cross-attribute the
    production data between the two units.
    """


async def poll_inverter(
    znp: ZNP,
    inv_id: str,
    ecu_id: str,
    expected_serial: str | None = None,
) -> DS3Reading:
    """Send a polling request to `inv_id` and decode the answer.

    Raises `PollError` if the burst is missing one of the success markers or
    the DS3 decoder cannot parse the AF_INCOMING_MSG body, and
    `SerialMismatchError` if `expected_serial` is given and the responding
    inverter reports a different serial.
    """
    cmd = build_poll_command(inv_id, ecu_id)
    try:
        burst = await znp.request(cmd)
    except ZNPError as err:
        raise PollError(f"transport failure while polling {inv_id}: {err}") from err
    if not burst:
        raise PollError(f"no answer from inverter {inv_id}")
    burst_u = burst.upper()
    if _OK_AF_DATA_REQUEST not in burst_u:
        raise PollError("AF_DATA_REQUEST did not succeed")
    if _OK_AF_DATA_CONFIRM not in burst_u:
        raise PollError("AF_DATA_CONFIRM did not succeed")
    if _AF_INCOMING_PREFIX not in burst_u:
        raise PollError("no AF_INCOMING_MSG in burst")
    try:
        reading = decode_ds3_frame(burst_u)
    except DS3DecodeError as err:
        raise PollError(f"DS3 decoder rejected the frame: {err}") from err
    if expected_serial is not None and reading.serial != expected_serial:
        raise SerialMismatchError(
            f"inverter at {inv_id} answered with serial {reading.serial}, "
            f"expected {expected_serial} — check the (serial ↔ invID) mapping "
            "of these two inverters in the integration config"
        )
    return reading


async def reboot_inverter(znp: ZNP, inv_id: str, ecu_id: str) -> None:
    """Send the proprietary reboot command to an inverter (`ZIGBEE_HELPERS.ino:147-193`)."""
    cmd = build_reboot_command(inv_id, ecu_id)
    try:
        await znp.send(cmd)
    except ZNPError as err:
        raise PollError(f"reboot failed for {inv_id}: {err}") from err
