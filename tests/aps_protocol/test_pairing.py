"""Tests for the pair handshake extraction logic.

The fixtures here are real bursts captured against live DS3 inverters on the
laptop test stack (2026-06-06 / 2026-06-07). They cover the three observed
shapes of step-2 reply:

1. Two `4481` frames — echo (SrcAddr=0x0000) + assignment (SrcAddr=new). The
   plugin must return the *assignment* SrcAddr, not anything else.
2. One `4481` frame only — echo, no assignment. The plugin must return
   `None` so the caller's retry loop can run.
3. Two `4481` frames where the assignment is a *neighbour*'s short address
   already paired (echoed from the mesh). The plugin must reject it.

The original inverter serial numbers seen in those captures have been
substituted with synthetic placeholders (`999999999991` / `999999999992` /
`999999999993`) to keep the test corpus free of personally-identifying
hardware IDs. The ZNP frame parser does not validate the trailing FCS byte
(see `_iter_znp_frames` which skips it), so substituting the SN ASCII bytes
inside the captured payload still exercises the parser logic end-to-end.
"""

from __future__ import annotations

from custom_components.aps_zigbee.aps_protocol.pairing import (
    _build_invid_blacklist,
    _extract_inv_id,
    _iter_znp_frames,
)


# Real burst from the laptop test stack, 2026-06-07 ~10:39:55. Step 2 reply
# when pairing the inverter SN 999999999991 (synthetic placeholder) against
# ECU D8A3011B9780, with inverter 999999999992 (synthetic, short addr B5AF)
# already on the network. The second 4481 has SrcAddr LE 3BA7 → BE A73B =
# the freshly assigned short address.
_BURST_GOOD = (
    "FE0164020067"
    "FE25448100000F0100001414012900F0C200000011"
    "999999999991"
    "A3D810FFFF80971B01A3D83BA70D49"
    "FE1C448100000101"
    "3BA7"
    "1414002900D1C700000008FFFF"
    "999999999991"
    "3BA7"
    "0EC0"
)

# Real burst from the laptop test stack, 2026-06-07 ~10:43:33. Step 2 reply
# when pairing the inverter SN 999999999993 (synthetic placeholder) — the
# inverter never sent its assignment frame, only the echo (SrcAddr=0x0000).
# The previous heuristic returned `D8A3` here (= the configured ECU's first
# 2 bytes reversed) which is wrong; the new extractor must return None so
# the pair attempt retries.
_BURST_ECHO_ONLY = (
    "FE0164020067"
    "FE25448100000F010000141401340077BA00000011"
    "999999999993"
    "A3D810FFFF80971B01A3D8AFB50DD3"
)


def test_iter_znp_frames_skips_srsp_and_yields_inner_payload() -> None:
    frames = list(_iter_znp_frames(_BURST_GOOD))
    cmds = [(c0, c1) for c0, c1, _ in frames]
    # SRSP (6402) + echo 4481 + assignment 4481
    assert cmds == [("64", "02"), ("44", "81"), ("44", "81")]
    # Echo's SrcAddr (bytes 4-5 of payload, LE) is 0000 (unassigned).
    assert frames[1][2][8:12] == "0000"
    # Assignment's SrcAddr LE is 3BA7 → BE A73B.
    assert frames[2][2][8:12] == "3BA7"


def test_iter_znp_frames_drops_truncated_tail() -> None:
    # The valid frame `FE0167...` plus a stray `FE` with no length.
    truncated = "FE0167010068FE"
    frames = list(_iter_znp_frames(truncated))
    assert frames == [("67", "01", "00")]


def test_build_invid_blacklist_derives_ecu_short_dynamically() -> None:
    bl_default = _build_invid_blacklist("D8A3011B9780", [])
    assert "D8A3" in bl_default  # default ECU's first 2 bytes (BE form)

    # A user with a different ECU gets a different ecu_short. Nothing hard
    # coded — the value tracks the configured ECU_ID.
    bl_other = _build_invid_blacklist("ABCD0123456789", [])
    assert "ABCD" in bl_other
    assert "D8A3" not in bl_other  # not the default user's bytes anymore

    # Broadcast / unassigned and the user-supplied known inverters are
    # always blacklisted regardless of ECU.
    bl_with_neighbours = _build_invid_blacklist("D8A3011B9780", ["B5AF", "a73b"])
    assert "0000" in bl_with_neighbours
    assert "FFFF" in bl_with_neighbours
    assert "B5AF" in bl_with_neighbours
    assert "A73B" in bl_with_neighbours  # known_inv_ids normalised to upper


def test_extract_inv_id_picks_assignment_frame_when_present() -> None:
    bl = _build_invid_blacklist("D8A3011B9780", ["B5AF"])
    assert _extract_inv_id(_BURST_GOOD, "999999999991", bl) == "A73B"


def test_extract_inv_id_returns_none_when_only_echo_received() -> None:
    bl = _build_invid_blacklist("D8A3011B9780", ["B5AF", "A73B"])
    # The bad burst's only 4481 has SrcAddr=0000 (echo). The old heuristic
    # would have returned "D8A3" (ECU bytes following the SN) — the new
    # extractor returns None so the caller can retry.
    assert _extract_inv_id(_BURST_ECHO_ONLY, "999999999993", bl) is None


def test_extract_inv_id_rejects_a_neighbours_short_address() -> None:
    # Same burst as `_BURST_GOOD` but the assignment SrcAddr matches an
    # already-paired neighbour (passed via known_inv_ids). Must skip it
    # rather than returning a duplicate invID.
    bl = _build_invid_blacklist("D8A3011B9780", ["A73B"])
    assert _extract_inv_id(_BURST_GOOD, "999999999991", bl) is None


def test_extract_inv_id_requires_serial_in_payload() -> None:
    # The good burst is for serial 999999999991. If the caller asks for a
    # different serial, the payload sanity check must reject the frame.
    bl = _build_invid_blacklist("D8A3011B9780", [])
    assert _extract_inv_id(_BURST_GOOD, "999999999999", bl) is None


def test_extract_inv_id_rejects_broadcast_zero_srcaddr() -> None:
    # A synthetic 4481 with SrcAddr 0x0000 and the serial in payload must
    # not be accepted (the inverter hasn't been assigned yet).
    payload = "0000" + "0101" + "0000" + "14" * 13 + "704000111111"
    # Wrap as a ZNP frame.
    length = len(payload) // 2
    frame = f"FE{length:02X}4481{payload}00"
    bl = _build_invid_blacklist("D8A3011B9780", [])
    assert _extract_inv_id(frame, "704000111111", bl) is None
