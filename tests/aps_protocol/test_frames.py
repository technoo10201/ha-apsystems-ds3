"""Tests for the proprietary-frame builders.

Gold values come from the comments in
`ESP32-read-APS-inverters-platformio/ZIGBEE_COORDINATOR.ino:50-67` and the
hand-traced poll/reboot/pair commands in the same repo.
"""

from __future__ import annotations

import pytest

from custom_components.aps_zigbee.aps_protocol.frames import (
    DEFAULT_ECU_ID,
    build_coordinator_init_commands,
    build_no_command,
    build_pair_commands,
    build_poll_command,
    build_reboot_command,
    ecu_id_reverse,
)


def test_ecu_id_reverse_matches_firmware() -> None:
    # `ECU_REVERSE()` in `ZIGBEE_HELPERS.ino:135-139`.
    assert ecu_id_reverse("D8A3011B9780") == "80971B01A3D8"


def test_ecu_id_reverse_validates_length() -> None:
    with pytest.raises(ValueError):
        ecu_id_reverse("DEADBEEF")  # 8 chars, not 12


def test_coordinator_init_sequence_against_firmware_comments() -> None:
    cmds = build_coordinator_init_commands(DEFAULT_ECU_ID)
    # The firmware's annotated frames are: `FE [LEN] [cmd hex...] [FCS]`. We
    # only care about the CMD+DATA portion, which is exactly what our builder
    # returns. The trailing `6700` is a SAPI device-info query required by
    # the Kadsol firmware to unlock AF_DATA_REQUEST_EXT (pair) frames.
    assert cmds == [
        "2605030103",
        "410000",
        "26050108FFFF80971B01A3D8",
        "2605870100",
        "26058302D8A3",
        "2605840400000100",
        "240014050F00010100020000150000",
        "2600",
        "6700",
    ]


def test_no_command_matches_firmware_literal() -> None:
    # The firmware snprintfs exactly this string with the reversed ECU id
    # spliced in. `ZIGBEE_COORDINATOR.ino:150`.
    assert build_no_command(DEFAULT_ECU_ID) == (
        "2401FFFF1414060001000F1E80971B01A3D8FBFB1100000D6030FBD3000000000000000004010281FEFE"
    )


def test_poll_command_layout() -> None:
    # `3A10` is the human/big-endian form (`aps_yc600` convention); the wire
    # frame swaps it to `103A`.
    cmd = build_poll_command("3A10", DEFAULT_ECU_ID)
    assert cmd == ("2401103A1414060001000F1380971B01A3D8FBFB06BB000000000000C1FEFE")


def test_poll_command_rejects_short_inv_id() -> None:
    with pytest.raises(ValueError):
        build_poll_command("ABC", DEFAULT_ECU_ID)


def test_reboot_command_layout() -> None:
    cmd = build_reboot_command("3A10", DEFAULT_ECU_ID)
    assert cmd == ("2401103A1414060001000F1380971B01A3D8FBFB06C1000000000000A6FEFE")


def test_pair_commands_layout() -> None:
    serial = "408000158211"
    cmds = build_pair_commands(serial, DEFAULT_ECU_ID)
    rev = "80971B01A3D8"
    ecu_short = "A3D8"  # ECU_ID[2:4] + ECU_ID[0:2], see ZIGBEE_PAIR.ino:60-62
    hdr = "24020FFFFFFFFFFFFFFFFF14FFFF14"
    assert cmds == [
        f"{hdr}0D0200000F1100{serial}FFFF10FFFF{rev}",
        f"{hdr}0C0201000F0600{serial}",
        f"{hdr}0F0102000F1100{serial}{ecu_short}10FFFF{rev}",
        f"{hdr}010103000F0600{rev}",
    ]


def test_pair_commands_validate_serial() -> None:
    with pytest.raises(ValueError):
        build_pair_commands("not12digits", DEFAULT_ECU_ID)
    with pytest.raises(ValueError):
        build_pair_commands("4080001582AB", DEFAULT_ECU_ID)
