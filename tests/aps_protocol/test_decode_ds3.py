"""DS3 frame decoder tests.

The reference fixture is the verbatim trace from
`ESP32-read-APS-inverters-platformio/AAA_DECODE.ino:93`. The expected values
are recomputed from the same scaling formulas as the firmware
(`AAA_DECODE.ino:111-138, 222, 244-298`) so a single typo in either place
will fail the test.
"""

from __future__ import annotations

import math

import pytest

from custom_components.aps_zigbee.aps_protocol.decode_ds3 import (
    DS3DecodeError,
    DS3Reading,
    decode_ds3_frame,
    derive_power,
)

# Full ZNP burst captured by the firmware for a real DS3 poll answer.
DS3_FIXTURE = (
    "FE0164010064"
    "FE034480001401D2"
    "FE0345C43A1000A8"
    "FE724481000006013A101414007100B57CFA00005E703000021300"
    "fbfb5cbbbb20000200e6ffff000000000000000006f506f9002e00340360138a17a70024001fffff"
    "054206900016f62b0018e451"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "3969fefe"
)


def test_decode_known_frame() -> None:
    r = decode_ds3_frame(DS3_FIXTURE)
    assert isinstance(r, DS3Reading)
    assert r.serial == "703000021300"
    # 0x71 = 113 → 113 * 100 / 255
    assert math.isclose(r.signal_quality_pct, 113 * 100 / 255, rel_tol=1e-6)
    # 0x06f5 = 1781 → /48
    assert math.isclose(r.vdc1_v, 1781 / 48, rel_tol=1e-6)
    # 0x06f9 = 1785 → /48
    assert math.isclose(r.vdc2_v, 1785 / 48, rel_tol=1e-6)
    # 0x002e = 46 → *0.0125
    assert math.isclose(r.idc1_a, 46 * 0.0125, rel_tol=1e-6)
    # 0x0034 = 52 → *0.0125
    assert math.isclose(r.idc2_a, 52 * 0.0125, rel_tol=1e-6)
    # 0x0360 = 864 → /3.8
    assert math.isclose(r.acv_v, 864 / 3.8, rel_tol=1e-6)
    # 0x138a = 5002 → /100
    assert math.isclose(r.freq_hz, 50.02, rel_tol=1e-6)
    # 0x17a7 = 6055
    assert r.timestamp_raw == 0x17A7
    # 0x0690 = 1680 → *0.0198 - 23.84
    assert math.isclose(r.temperature_c, 1680 * 0.0198 - 23.84, rel_tol=1e-6)
    # /100000 * 1.66 against the raw uint32, see firmware AAA_DECODE.ino:271-272.
    assert math.isclose(r.energy_p1_wh, int("0016f62b", 16) / 100000 * 1.66, rel_tol=1e-6)
    assert math.isclose(r.energy_p2_wh, int("0018e451", 16) / 100000 * 1.66, rel_tol=1e-6)


def test_decode_known_frame_yields_physical_sanity() -> None:
    r = decode_ds3_frame(DS3_FIXTURE)
    # Rough cross-checks against what the inverter could plausibly emit.
    assert 0.0 <= r.signal_quality_pct <= 100.0
    assert 0.0 <= r.vdc1_v < 60.0
    assert 0.0 <= r.vdc2_v < 60.0
    assert 100.0 <= r.acv_v < 280.0
    assert 49.0 <= r.freq_hz <= 51.0


def test_decode_rejects_burst_without_marker() -> None:
    with pytest.raises(DS3DecodeError):
        decode_ds3_frame("FE0164010064DEADBEEF")


def test_decode_rejects_truncated_frame() -> None:
    with pytest.raises(DS3DecodeError):
        decode_ds3_frame("44810000" + "DEADBEEF" * 4)


def test_decode_is_case_insensitive() -> None:
    upper = decode_ds3_frame(DS3_FIXTURE.upper())
    lower = decode_ds3_frame(DS3_FIXTURE.lower())
    assert upper == lower


def test_derive_power_first_reading_uses_full_energy() -> None:
    r = _reading(timestamp=3600, energy_p1=50.0, energy_p2=30.0)
    p1, p2, total = derive_power(None, r)
    # 50 Wh over 3600 s = 50 W; 30 Wh over 3600 s = 30 W
    assert math.isclose(p1, 50.0)
    assert math.isclose(p2, 30.0)
    assert math.isclose(total, 80.0)


def test_derive_power_uses_delta_between_readings() -> None:
    a = _reading(timestamp=1000, energy_p1=10.0, energy_p2=5.0)
    b = _reading(timestamp=1900, energy_p1=12.5, energy_p2=6.5)
    # Δt = 900 s, ΔE1 = 2.5 Wh → 10 W, ΔE2 = 1.5 Wh → 6 W
    p1, p2, total = derive_power(a, b)
    assert math.isclose(p1, 10.0, rel_tol=1e-9)
    assert math.isclose(p2, 6.0, rel_tol=1e-9)
    assert math.isclose(total, 16.0, rel_tol=1e-9)


def test_derive_power_detects_inverter_reset() -> None:
    a = _reading(timestamp=5000, energy_p1=100.0, energy_p2=80.0)
    # Timestamp went backwards → inverter rebooted; we treat current values as
    # the increment since the new boot.
    b = _reading(timestamp=720, energy_p1=4.0, energy_p2=2.0)
    p1, p2, total = derive_power(a, b)
    assert math.isclose(p1, 4.0 / 720 * 3600)
    assert math.isclose(p2, 2.0 / 720 * 3600)
    assert math.isclose(total, p1 + p2)


def test_derive_power_zero_dt_returns_zeros() -> None:
    a = _reading(timestamp=1000, energy_p1=10.0, energy_p2=5.0)
    b = _reading(timestamp=1000, energy_p1=12.5, energy_p2=6.5)
    assert derive_power(a, b) == (0.0, 0.0, 0.0)


def _reading(*, timestamp: int, energy_p1: float, energy_p2: float) -> DS3Reading:
    return DS3Reading(
        serial="000000000000",
        signal_quality_pct=50.0,
        vdc1_v=35.0,
        vdc2_v=35.0,
        idc1_a=1.0,
        idc2_a=1.0,
        acv_v=230.0,
        freq_hz=50.0,
        temperature_c=25.0,
        timestamp_raw=timestamp,
        energy_p1_wh=energy_p1,
        energy_p2_wh=energy_p2,
    )
