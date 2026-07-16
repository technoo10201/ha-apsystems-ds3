"""Builders and constants for the APsystems Zigbee proprietary protocol.

All builders return hex strings (uppercase or lowercase mixed is fine — they are
re-normalised at the wire layer). The ZNP framing (preamble `FE`, length byte,
trailing XOR checksum) is added by `znp.ZNP.send`, **not** here.

The reference implementation we port is patience4711/ESP32-read-APS-inverters
(`ESP32-read-APS-inverters-platformio/ZIGBEE_*.ino` and `AAA_DECODE.ino`).
"""

from __future__ import annotations

from dataclasses import dataclass

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


# --- SAPI_GET_DEVICE_INFO (cmd 0x6700) ---

# CC2530 DeviceState values reported in the SAPI_GET_DEVICE_INFO response.
DEVICE_STATE_DEV_HOLD = 0x00   # radio not yet joined / network down
DEVICE_STATE_ZB_COORD = 0x09   # running as Zigbee coordinator (network up)

# Short address owned by the coordinator when the network is operational.
COORDINATOR_SHORT_ADDR = "0000"


def build_device_info_command() -> str:
    """Return the SAPI_GET_DEVICE_INFO_REQ command (0x6700).

    Network-state probe for the CC2530: the response carries the coordinator's
    IEEE address, short address, DeviceType, DeviceState (0x09 = DEV_ZB_COORD
    when the Zigbee network is up; 0x00 = DEV_HOLD when the radio has not yet
    joined or has lost its network) and the count of associated devices.
    """
    return "6700"


@dataclass(frozen=True)
class CoordinatorDeviceInfo:
    """Parsed SAPI_GET_DEVICE_INFO_RSP (cmd 0x6700) frame."""

    status: int
    ieee: str        # big-endian hex string, 16 chars (8 bytes)
    short_addr: str  # big-endian hex string, 4 chars (e.g. "0000" or "FFFE")
    device_type: int
    device_state: int
    num_assoc: int

    @property
    def network_up(self) -> bool:
        """True when the Zigbee network is fully operational.

        The CC2530 must be acting as coordinator (DeviceState=0x09) *and* have
        claimed the coordinator short address (0x0000).  Any other combination
        means DEV_HOLD or an unexpected intermediate state.
        """
        return (
            self.device_state == DEVICE_STATE_ZB_COORD
            and self.short_addr == COORDINATOR_SHORT_ADDR
        )


def parse_device_info(burst: str) -> CoordinatorDeviceInfo | None:
    """Extract the last SAPI_GET_DEVICE_INFO_RSP (cmd 6700) from a ZNP burst.

    Scans `burst` for ZNP frames whose CMD bytes are 0x67 0x00 and whose
    LEN field covers at least the 14-byte SAPI_GET_DEVICE_INFO payload.
    Returns the *last* valid match as a `CoordinatorDeviceInfo`, or None when
    no complete 6700 frame is present.

    ZNP frame layout (hex string): FE LEN(1) CMD0(1) CMD1(1) payload(LEN) FCS(1).

    Payload layout (14 bytes):
        Status    (1 B)
        IEEE      (8 B, little-endian on wire → stored big-endian)
        ShortAddr (2 B, little-endian on wire → stored big-endian)
        DeviceType   (1 B)
        DeviceState  (1 B)
        NumAssoc     (1 B)

    Technique mirrors `extract_route`: scan forward, accumulate the last
    valid hit so burst noise before the real frame is silently skipped.
    """
    burst_u = burst.upper().replace(" ", "")
    result: CoordinatorDeviceInfo | None = None
    pos = 0
    while True:
        pos = burst_u.find("FE", pos)
        if pos == -1:
            break
        # Minimum: FE(2) + LEN(2) + CMD(4) = 8 chars before any payload.
        if pos + 8 > len(burst_u):
            break
        try:
            length = int(burst_u[pos + 2 : pos + 4], 16)
        except ValueError:
            pos += 2
            continue
        cmd = burst_u[pos + 4 : pos + 8]
        if cmd != "6700":
            pos += 2
            continue
        # The SAPI_GET_DEVICE_INFO payload is always 14 bytes (0x0E).
        if length < 0x0E:
            pos += 2
            continue
        payload_start = pos + 8
        payload_end = payload_start + length * 2
        # +2 for the FCS byte that must be present for a complete frame.
        if payload_end + 2 > len(burst_u):
            # Truncated frame — stop scanning (nothing useful follows).
            break
        payload = burst_u[payload_start:payload_end]
        status = int(payload[0:2], 16)
        # IEEE: 8 bytes LE → reverse each byte pair to obtain BE.
        ieee_le = payload[2:18]
        ieee_be = "".join(ieee_le[i : i + 2] for i in range(14, -2, -2))
        # ShortAddr: 2 bytes LE → swap using the existing helper.
        short_le = payload[18:22]
        short_be = swap_inv_id_bytes(short_le)
        device_type = int(payload[22:24], 16)
        device_state = int(payload[24:26], 16)
        num_assoc = int(payload[26:28], 16)
        result = CoordinatorDeviceInfo(
            status=status,
            ieee=ieee_be,
            short_addr=short_be,
            device_type=device_type,
            device_state=device_state,
            num_assoc=num_assoc,
        )
        pos = payload_end + 2  # step over FCS
    return result


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
