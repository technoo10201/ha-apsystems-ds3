"""Pairing handshake for APsystems inverters.

The proprietary protocol pairs an inverter (identified by its 12-digit serial
number, printed on the device) to the coordinator by sending 4 specific
commands in sequence. After commands 1 and 2 the inverter echoes back a frame
that contains the serial **plus** the 4 hex chars (16-bit short address) that
become its `invID` for all future polling.

Reference: `ZIGBEE_PAIR.ino:53-174`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Iterator

from .frames import build_pair_commands, swap_inv_id_bytes
from .znp import ZNP, ZNPError

_LOGGER = logging.getLogger(__name__)

# After each pair command the firmware waits 1.5 s for the inverter to answer.
# We give it a touch more to cover slower radios / longer paths.
_PAIR_STEP_DELAY_S = 2.0

# We retry the full 4-command sequence a few times — pairing is one-shot from
# the user's perspective and an inverter that didn't catch the first attempt
# sometimes catches the second. Mirrors common practice with the upstream
# firmware's "press pair again" UX.
# Keep retries low: in practice, when an inverter answers it does so on
# attempt 1 — extra rounds only buy time when the radio is genuinely deaf
# (firmware doesn't accept AF_DATA_REQUEST_EXT, range issues, etc.) and
# pile up frustration during config-flow setup.
_PAIR_ATTEMPTS = 2
_PAIR_ATTEMPT_DELAY_S = 2.0


class PairingError(Exception):
    """Raised when an inverter does not echo a usable short address."""


async def pair_inverter(
    znp: ZNP,
    serial: str,
    ecu_id: str,
    known_inv_ids: Iterable[str] = (),
) -> str:
    """Run the 4-command handshake and return the inverter short address.

    `serial` is the 12-digit decimal serial printed on the inverter. The
    coordinator must already be up (see `coordinator.init_coordinator`).
    `known_inv_ids` is the list of short addresses already assigned to other
    inverters in this config entry — needed so a fresh pair attempt doesn't
    silently re-use a neighbour's address echoed in the mesh.

    Important: the inverter answers each pair command **about a second after**
    the dongle's SRSP (the immediate command-acknowledgement). If we read
    right after sending — as `ZNP.request` does — we only catch the SRSP and
    miss the AF_INCOMING_MSG that carries the serial and the short address.
    We therefore send, sleep, then drain whatever has accumulated, matching
    the upstream firmware's `sendZB(); delay(1500); readZB();` pattern from
    `ZIGBEE_PAIR.ino:85-105`.
    """
    commands = build_pair_commands(serial, ecu_id)
    blacklist = _build_invid_blacklist(ecu_id, known_inv_ids)
    found_inv_id: str | None = None
    last_replies: list[str] = []

    for attempt in range(1, _PAIR_ATTEMPTS + 1):
        last_replies.clear()
        found_inv_id = None
        _LOGGER.debug("pair attempt %s/%s for %s", attempt, _PAIR_ATTEMPTS, serial)
        for step, cmd in enumerate(commands):
            try:
                await znp.send(cmd)
            except ZNPError as err:
                raise PairingError(f"step {step} send failed: {err}") from err
            await asyncio.sleep(_PAIR_STEP_DELAY_S)
            try:
                reply = await znp.recv()
            except ZNPError as err:
                _LOGGER.debug("pair step %s recv failed: %s", step, err)
                reply = ""
            last_replies.append(reply)
            _LOGGER.debug("pair attempt %s step %s rx=%s", attempt, step, reply)
            # We scan every step's burst: an AF_INCOMING_MSG with a non-zero,
            # non-broadcast SrcAddr distinct from our ECU and from any
            # already-paired inverter is the inverter announcing itself from
            # its newly assigned address. The 4-frame handshake must always
            # run to completion so the inverter fully binds itself to our
            # ECU at the firmware level (stopping early was the cause of the
            # "AF_DATA_CONFIRM 0xCD = ZNwkNoRoute" failure mode observed on
            # DS3 inverters on 2026-06-06).
            if reply:
                candidate = _extract_inv_id(reply, serial, blacklist)
                if candidate is not None:
                    found_inv_id = candidate
        if found_inv_id is not None:
            _LOGGER.debug(
                "pair attempt %s succeeded for %s (inv_id=%s)",
                attempt,
                serial,
                found_inv_id,
            )
            break
        _LOGGER.debug(
            "pair attempt %s/%s did not yield an inv_id (replies: %s); will retry",
            attempt,
            _PAIR_ATTEMPTS,
            [r[:20] for r in last_replies],
        )
        await asyncio.sleep(_PAIR_ATTEMPT_DELAY_S)

    if found_inv_id is None:
        raise PairingError(
            f"no usable short address announced by inverter {serial} after "
            f"{_PAIR_ATTEMPTS} attempts — the inverter never sent an "
            "AF_INCOMING_MSG from a freshly assigned (non-zero, non-ECU, "
            "non-neighbour) short address. Retry the pair once the inverter "
            "is producing (full daylight) or move the dongle closer."
        )
    return found_inv_id


def _build_invid_blacklist(
    ecu_id: str, known_inv_ids: Iterable[str]
) -> frozenset[str]:
    """Return the set of short addresses that are NOT valid pair outcomes.

    Reserved: `0000` (the unassigned / broadcast-source value the inverter
    uses BEFORE it has been bound), `FFFF` (the all-1s broadcast).
    Algorithmic, NOT hardcoded — the ECU-derived entry is recomputed from
    the user's configured `ecu_id`:
        `ecu_short` on the wire is `ecu_id[2:4] + ecu_id[0:2]`
        (see `frames.build_pair_commands:110`); its human BE form is the
        same swap applied once more — so for the default `D8A3011B9780` we
        get `D8A3`. Any other user's `ecu_id` will yield a different value.
    `known_inv_ids` covers neighbour inverters already bound to this ECU
    so a stray echo from them in the pair burst can't pollute the new
    inverter's invID.
    """
    ecu_short_wire = ecu_id[2:4] + ecu_id[0:2]
    ecu_short_be = swap_inv_id_bytes(ecu_short_wire).upper()
    return frozenset(
        {"0000", "FFFF", ecu_short_be, *(i.upper() for i in known_inv_ids)}
    )


def _extract_inv_id(
    reply_hex: str, serial: str, blacklist: frozenset[str]
) -> str | None:
    """Return the inverter's freshly assigned short address (human BE form).

    Walks the burst as ZNP frames and looks for an `AF_INCOMING_MSG`
    (`cmd0=0x44 cmd1=0x81`) whose SrcAddr field — bytes 4-5 of the
    payload, little-endian — decodes to a short address that:

      1. is NOT in `blacklist` (covers `0000`, `FFFF`, the ECU's own short
         address derived from `ecu_id`, and any already-paired inverter),
      2. comes from a payload containing the target `serial` (sanity check
         so a stray frame from another network device can't pollute the
         result).

    Returns `None` when no such frame is in the burst. The caller treats
    that as "this attempt didn't reach the inverter, retry". This replaces
    the previous best-effort "grab 4 hex chars after the serial echo"
    heuristic which would return ECU bytes or a neighbour's address when
    the inverter never sent its assignment frame.
    """
    needle = serial.upper()
    for cmd0, cmd1, payload_hex in _iter_znp_frames(reply_hex):
        if (cmd0, cmd1) != ("44", "81"):
            continue
        if len(payload_hex) < 12:
            continue
        # AF_INCOMING_MSG header: GroupID(2) ClusterID(2) SrcAddr(2,LE) …
        srcaddr_wire = payload_hex[8:12]
        srcaddr_be = swap_inv_id_bytes(srcaddr_wire).upper()
        if srcaddr_be in blacklist:
            continue
        if needle not in payload_hex:
            continue
        return srcaddr_be
    return None


def _iter_znp_frames(reply_hex: str) -> Iterator[tuple[str, str, str]]:
    """Yield `(cmd0, cmd1, payload_hex)` for each well-formed ZNP frame.

    Frame layout (per Texas Instruments ZNP spec):
        `FE <len> <cmd0> <cmd1> <payload of len bytes> <fcs>`
    where every byte is encoded as 2 hex chars in `reply_hex`. Malformed
    or truncated frames at the tail of the burst are silently dropped —
    serial reads sometimes splice partial frames.
    """
    reply = reply_hex.upper()
    i = 0
    while i < len(reply):
        if reply[i : i + 2] != "FE":
            i += 2
            continue
        if i + 10 > len(reply):
            return
        try:
            length = int(reply[i + 2 : i + 4], 16)
        except ValueError:
            i += 2
            continue
        payload_start = i + 8
        payload_end = payload_start + 2 * length
        if payload_end + 2 > len(reply):
            return
        cmd0 = reply[i + 4 : i + 6]
        cmd1 = reply[i + 6 : i + 8]
        payload_hex = reply[payload_start:payload_end]
        yield cmd0, cmd1, payload_hex
        i = payload_end + 2  # skip FCS
