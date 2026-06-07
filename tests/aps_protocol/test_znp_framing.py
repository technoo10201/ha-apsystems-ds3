"""Verify ZNP wire framing against the gold examples from the firmware.

The firmware logs each sent frame fully composed (FE + LEN + CMD/DATA + FCS).
Those are reproduced verbatim in `ZIGBEE_COORDINATOR.ino:50-67` and make
perfect framing fixtures.
"""

from __future__ import annotations

import pytest

from custom_components.aps_zigbee.aps_protocol.znp import _build_frame, compute_fcs


@pytest.mark.parametrize(
    ("payload", "expected_frame"),
    [
        ("2605030103", "FE03260503010321"),
        ("410000", "FE0141000040"),
        ("26050108FFFF80971B01A3D8", "FE0A26050108FFFF80971B01A3D856"),
        ("2605870100", "FE032605870100A6"),
        ("26058302D8A3", "FE0426058302D8A3DD"),
        ("2605840400000100", "FE062605840400000100A4"),
        (
            "240014050F00010100020000150000",
            "FE0D240014050F0001010002000015000020",
        ),
        ("2600", "FE00260026"),
    ],
)
def test_build_frame_matches_firmware(payload: str, expected_frame: str) -> None:
    assert _build_frame(payload).hex().upper() == expected_frame


def test_compute_fcs_matches_known_payloads() -> None:
    assert compute_fcs("2600") == 0x26
    assert compute_fcs("410000") == 0x40
    assert compute_fcs("2605030103") == 0x21


def test_build_frame_rejects_under_two_bytes() -> None:
    with pytest.raises(ValueError):
        _build_frame("FE")  # single byte payload
