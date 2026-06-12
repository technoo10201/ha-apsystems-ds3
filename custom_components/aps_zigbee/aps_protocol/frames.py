"""Builders and constants for the APsystems Zigbee proprietary protocol.

All builders return hex strings (uppercase or lowercase mixed is fine — they are
re-normalised at the wire layer). The ZNP framing (preamble `FE`, length byte,
trailing XOR checksum) is added by `znp.ZNP.send`, **not** here.

The reference implementation we port is patience4711/ESP32-read-APS-inverters
(`ESP32-read-APS-inverters-platformio/ZIGBEE_*.ino` and `AAA_DECODE.ino`).
"""

from __future__ import annotations

# The APS firmware uses an arbitrary 6-byte coordinator identity. Any value
# accepted; once an inverter is paired with one ECU_ID it remembers it, so
# changing it forces re-pairing. The default matches the upstream firmware.
DEFAULT_ECU_ID = "D8A3011B9780"

# Wildcard short address used by `sendNO` / pairing broadcast.
WILDCARD_SHORT_ADDR = "FFFF"

# Inverter type indices used in the firmware. We only target DS3.
INV_TYPE_YC600 = 0
INV_TYPE_QS1 = 1
INV_TYPE_DS3 = 2


def ecu_id_reverse(ecu_id: str) -> str:
    """Return the byte-reversed ECU id (little-endian on the wire).

    `D8A3011B9780` → `80971B01A3D8`. The CC2530 ZNP expects the IEEE address in
    little-endian; the firmware does the same transformation in
    `ZIGBEE_HELPERS.ino:135-139`.
    """
    if len(ecu_id) != 12:
        raise ValueError(f"ECU id must be 12 hex chars (6 bytes), got {ecu_id!r}")
    return "".join(ecu_id[i : i + 2] for i in range(10, -2, -2))


def swap_inv_id_bytes(inv_id: str) -> str:
    """Swap the two bytes of a Zigbee short address: `B745` ↔ `45B7`.

    The proprietary protocol expects the 16-bit short address in
    little-endian on the wire (LSB first), while the upstream `aps_yc600.py`
    library — and therefore the user-facing string — exposes it in
    big-endian (MSB first, the form you would read on the inverter label).
    This helper bridges the two so the integration accepts/stores the
    "human" form and emits the "wire" form transparently.
    """
    if len(inv_id) != 4:
        raise ValueError(f"inv_id must be 4 hex chars (2 bytes), got {inv_id!r}")
    return inv_id[2:4] + inv_id[0:2]


def build_coordinator_init_commands(ecu_id: str) -> list[str]:
    """Return the 9 hex-payload commands that bring up the CC2530 coordinator.

    See `ZIGBEE_COORDINATOR.ino:44-138` and the upstream `aps_yc600.py`
    `start_coordinator` (lines 596-621). The `6700`
    (`SAPI_GET_DEVICE_INFO_REQ`) at the end is **required** for the Kadsol
    firmware: without it the radio refuses subsequent `AF_DATA_REQUEST_EXT`
    pair frames with status `0x02` (INVALID_PARAMETER) and never emits them
    on the air. Each command is the CMD+DATA hex string; the ZNP layer adds
    the FE/LEN/FCS framing.
    """
    rev = ecu_id_reverse(ecu_id)
    short = ecu_id[0:4]  # the first 2 bytes (in firmware-textual order)
    return [
        "2605030103",  # ZB_WRITE_CONFIGURATION
        "410000",  # ZB_SYS_RESET_REQ
        f"26050108FFFF{rev}",  # set ext. addr (IEEE)
        "2605870100",
        f"26058302{short}",  # set short addr (first 2 bytes of ECU id)
        "2605840400000100",
        "240014050F00010100020000150000",  # AF_REGISTER endpoint
        "2600",  # ZB_START_REQUEST
        "6700",  # SAPI_GET_DEVICE_INFO_REQ — unlocks AF_DATA_REQUEST_EXT
    ]


def build_no_command(ecu_id: str) -> str:
    """The "normal operation" command sent right after coordinator init.

    Verbatim from `ZIGBEE_COORDINATOR.ino:150`.
    """
    rev = ecu_id_reverse(ecu_id)
    return f"2401FFFF1414060001000F1E{rev}FBFB1100000D6030FBD3000000000000000004010281FEFE"


def build_check_alive_command() -> str:
    """ZB_READ_CONFIGURATION on item 0 — used as a coordinator liveness probe.

    Cf. `ZIGBEE_HEALTH.ino` and the comments in `ZIGBEE_COORDINATOR.ino:1-16`.
    """
    return "2700"


def build_pair_commands(serial: str, ecu_id: str) -> list[str]:
    """Return the 4 pairing-handshake commands for an inverter `serial`.

    Verbatim from `ZIGBEE_PAIR.ino:66-110`. The inverter must answer commands 1
    and 2 with a frame containing its 12-digit serial; the 4 chars following
    that serial give us its 16-bit short address (the `invID`).

    `serial` must be the 12-character ASCII-decimal serial number printed on
    the device (e.g. ``"408000158211"``).
    """
    if len(serial) != 12 or not serial.isdigit():
        raise ValueError(f"serial must be 12 decimal digits, got {serial!r}")
    rev = ecu_id_reverse(ecu_id)
    ecu_short = ecu_id[2:4] + ecu_id[0:2]  # see ZIGBEE_PAIR.ino:60-62
    header = "24020FFFFFFFFFFFFFFFFF14FFFF14"
    return [
        f"{header}0D0200000F1100{serial}FFFF10FFFF{rev}",
        f"{header}0C0201000F0600{serial}",
        f"{header}0F0102000F1100{serial}{ecu_short}10FFFF{rev}",
        f"{header}010103000F0600{rev}",
    ]


def build_poll_command(inv_id: str, ecu_id: str) -> str:
    """Return the polling request payload for an already-paired inverter.

    `inv_id` is the 4-char hex short address in *human* (big-endian) form,
    e.g. `B745`. We swap to wire (little-endian) order before emission to
    match the upstream `aps_yc600.poll_inverter` (line 482-485) and the
    ESP32 firmware after byte-reversal.

    Reference: `ZIGBEE_POLLING.ino:13` and `aps_yc600.py:482`.
    """
    wire_inv_id = swap_inv_id_bytes(inv_id)
    rev = ecu_id_reverse(ecu_id)
    return f"2401{wire_inv_id}1414060001000F13{rev}FBFB06BB000000000000C1FEFE"


def build_reboot_command(inv_id: str, ecu_id: str) -> str:
    """Return the reboot-inverter payload.

    Same big-endian → little-endian swap as `build_poll_command`.
    Reference: `ZIGBEE_HELPERS.ino:163-174`.
    """
    wire_inv_id = swap_inv_id_bytes(inv_id)
    rev = ecu_id_reverse(ecu_id)
    return f"2401{wire_inv_id}1414060001000F13{rev}FBFB06C1000000000000A6FEFE"


def build_zdo_mgmt_lqi_request(dst_addr: str = "0000", start_index: int = 0) -> str:
    """Return the `ZDO_MGMT_LQI_REQ` payload (cmd 0x2531).

    Asks a node (typically the dongle itself, `dst_addr=0000`) for its
    neighbour table — the list of Zigbee devices it can hear directly. The
    response (cmd 0x4531) carries `(short_addr, ext_addr, depth, lqi, ...)`
    for every entry, so we can see exactly which inverter short addresses
    are currently on the mesh, independent of whatever the config entry
    claims.

    `dst_addr` is the 4-char hex short address in *human* (big-endian)
    form; `start_index` is the cursor for pagination (0 = start).

    Spec: Texas Instruments Z-Stack ZNP API, `ZDO_MGMT_LQI_REQ`.
    """
    wire_dst = swap_inv_id_bytes(dst_addr)
    return f"2531{wire_dst}{start_index:02X}"


def build_zdo_mgmt_rtg_request(dst_addr: str = "0000", start_index: int = 0) -> str:
    """Return the `ZDO_MGMT_RTG_REQ` payload (cmd 0x2532).

    Sibling of `build_zdo_mgmt_lqi_request` — asks for the routing table
    instead of the neighbour table. Routes show `dst_short_addr` and
    `next_hop_short_addr` for every target the dongle has discovered (not
    just direct neighbours), so this is what reveals mesh-reachable
    inverters past the first hop.

    Spec: Texas Instruments Z-Stack ZNP API, `ZDO_MGMT_RTG_REQ`.
    """
    wire_dst = swap_inv_id_bytes(dst_addr)
    return f"2532{wire_dst}{start_index:02X}"


def extract_route(burst: str, inv_id: str) -> list[str] | None:
    """Pull the mesh route to `inv_id` out of a poll burst, if announced.

    While routing a unicast, the dongle's firmware emits unsolicited
    `ZDO_SRC_RTG_IND` frames (cmd 0x45C4) describing the source route it
    used: `FE <len> 45 C4 <dst LE:2> <relay_count:1> <relays LE:2 each> <fcs>`.
    A direct neighbour yields `relay_count=0`; a 2-hop inverter lists its
    relays in the order the firmware reports them (inverter side first).

    Returns the relay short addresses in human (big-endian) form for the
    **last** 0x45C4 frame matching `inv_id`, `[]` for a direct link, or
    `None` when the burst contains no route indication for that address
    (the firmware does not emit one on every poll).
    """
    wire_dst = swap_inv_id_bytes(inv_id).upper()
    burst_u = burst.upper().replace(" ", "")
    relays: list[str] | None = None
    pos = 0
    while True:
        pos = burst_u.find("45C4" + wire_dst, pos)
        if pos == -1:
            break
        # The length byte sits right before "45C4" (frame = FE LEN 45 C4 ...).
        if pos < 4 or burst_u[pos - 4 : pos - 2] != "FE":
            pos += 4
            continue
        length = int(burst_u[pos - 2 : pos], 16)
        count_pos = pos + 8                       # after cmd(4) + dst(4)
        if count_pos + 2 > len(burst_u):
            break
        relay_count = int(burst_u[count_pos : count_pos + 2], 16)
        # Sanity: LEN must cover dst(2) + count(1) + relays(2*count).
        if length != 3 + 2 * relay_count:
            pos += 4
            continue
        hop_hex = burst_u[count_pos + 2 : count_pos + 2 + 4 * relay_count]
        if len(hop_hex) < 4 * relay_count:
            break
        relays = [
            swap_inv_id_bytes(hop_hex[i : i + 4])
            for i in range(0, len(hop_hex), 4)
        ]
        pos += 4
    return relays


def build_zdo_mgmt_permit_join_request(
    duration_s: int = 180, dst_addr: str = "FFFC", legacy: bool = False
) -> str:
    """Return the `ZDO_MGMT_PERMIT_JOIN_REQ` payload (cmd 0x2536).

    Broadcasts (default dst `FFFC` = all routers) a standard Zigbee
    permit-join window so an **unjoined** inverter can associate through
    any mesh router — its roof neighbours — instead of needing a direct
    radio path to the dongle for the pair handshake. This is presumably
    what the official APS ECU does when commissioning a whole roof from
    the garage; the reverse-engineered upstream firmware never sends it.

    Two wire formats exist: modern Z-Stack prepends an `AddrMode` byte
    (0x0F = broadcast) before the destination, the older ZNP 1.x format
    starts directly at the destination. Pass `legacy=True` for the latter
    — the caller can try both and keep whichever the firmware accepts.

    Spec: Texas Instruments Z-Stack ZNP API, `ZDO_MGMT_PERMIT_JOIN_REQ`.
    """
    duration = max(1, min(0xFE, duration_s))
    wire_dst = swap_inv_id_bytes(dst_addr)
    if legacy:
        return f"2536{wire_dst}{duration:02X}00"
    return f"25360F{wire_dst}{duration:02X}00"
