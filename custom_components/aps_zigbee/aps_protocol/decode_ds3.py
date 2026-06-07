"""Stateless decoder for APsystems DS3 inverter response frames.

The DS3 inverter answers a polling request with several concatenated ZNP
frames. Everything we care about lives in the `AF_INCOMING_MSG` block, which
is found by splitting the raw hex burst on the marker ``"44810000"`` —
identical to the way the upstream firmware does it in
`AAA_DECODE.ino:68`.

Once split, the suffix (`tail`) starts with the inverter serial (6 bytes →
12 hex chars). Field offsets below are expressed in **bytes** of `tail`,
matching the ASCII-art table in `AAA_DECODE.ino:78-101`.

Reference offsets / scaling factors come from `AAA_DECODE.ino:111-138, 222,
244-298`. Power is **not** computed here — the inverter only reports
cumulative energy and a 16-bit timestamp; deriving instantaneous power needs
the previous reading. That stateful step belongs to the Home Assistant
coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Hex string fragment that prefixes the inverter data block.
AF_INCOMING_MARKER: Final[str] = "44810000"

# Field offsets *in bytes of tail* (so multiply by 2 for hex-string slicing).
# `tail[0:6]`  = inverter serial (6 bytes / 12 hex chars).
# The firmware then jumps to `s_d = tail[30 chars:] = tail[15 bytes:]` before
# reading numeric fields, hence every offset below is given relative to the
# **s_d window** (= tail starting from byte 15).
_OFFSET_SIGQUALITY = 7  # in tail, before the s_d window — 1 byte
_OFFSET_VDC1 = 26  # in s_d, 2 bytes
_OFFSET_VDC2 = 28
_OFFSET_IDC1 = 30
_OFFSET_IDC2 = 32
_OFFSET_ACV = 34
_OFFSET_FREQ = 36
_OFFSET_TIMESTAMP = 38
_OFFSET_TEMP = 48
_OFFSET_ENERGY_P1 = 50  # 4 bytes each
_OFFSET_ENERGY_P2 = 54

_SD_START_BYTE = 15  # = 30 hex chars, see `AAA_DECODE.ino:111`

# Minimum useful tail length, post-split — anything shorter means a truncated
# or partial frame. The firmware uses 223 as the threshold on the full message
# (`AAA_DECODE.ino:60`); after removing the prefix we expect ~223 - 30 = 193+.
_MIN_TAIL_HEX_LEN = 2 * (_OFFSET_ENERGY_P2 + 4 + _SD_START_BYTE)


class DS3DecodeError(ValueError):
    """The response did not contain a decodable DS3 frame."""


@dataclass(frozen=True, slots=True)
class DS3Reading:
    """A single DS3 polling readout.

    Energy values are **cumulative** as reported by the inverter (Wh). They
    reset whenever the inverter loses power. The timestamp wraps at
    16-bit — only differences between consecutive readings carry meaning.
    """

    serial: str
    signal_quality_pct: float
    vdc1_v: float
    vdc2_v: float
    idc1_a: float
    idc2_a: float
    acv_v: float
    freq_hz: float
    temperature_c: float
    timestamp_raw: int
    energy_p1_wh: float
    energy_p2_wh: float


def decode_ds3_frame(raw_hex: str) -> DS3Reading:
    """Parse a full polling response and return the inverter data.

    `raw_hex` is the uppercase or mixed-case hex string returned by
    `ZNP.recv()`. We grep for `44810000`, take the suffix, and pull out the
    DS3 fields with the same offsets and scaling as the upstream firmware.

    Raises `DS3DecodeError` if no AF_INCOMING_MSG block is found or the
    frame is too short to be valid inverter data.
    """
    normalised = raw_hex.upper().replace(" ", "")
    marker_pos = normalised.rfind(AF_INCOMING_MARKER)
    if marker_pos == -1:
        raise DS3DecodeError(f"no {AF_INCOMING_MARKER} marker in response")
    tail = normalised[marker_pos + len(AF_INCOMING_MARKER) :]
    if len(tail) < _MIN_TAIL_HEX_LEN:
        raise DS3DecodeError(f"tail too short to be a DS3 frame ({len(tail)} hex chars)")

    sig_q_raw = _read_uint(tail, _OFFSET_SIGQUALITY, 1)
    s_d = tail[_SD_START_BYTE * 2 :]
    serial = s_d[:12]

    vdc1 = _read_uint(s_d, _OFFSET_VDC1, 2) / 48.0
    vdc2 = _read_uint(s_d, _OFFSET_VDC2, 2) / 48.0
    idc1 = _read_uint(s_d, _OFFSET_IDC1, 2) * 0.0125
    idc2 = _read_uint(s_d, _OFFSET_IDC2, 2) * 0.0125
    acv = _read_uint(s_d, _OFFSET_ACV, 2) / 3.8
    freq = _read_uint(s_d, _OFFSET_FREQ, 2) / 100.0
    timestamp = _read_uint(s_d, _OFFSET_TIMESTAMP, 2)
    temp = _read_uint(s_d, _OFFSET_TEMP, 2) * 0.0198 - 23.84
    en_p1_raw = _read_uint(s_d, _OFFSET_ENERGY_P1, 4)
    en_p2_raw = _read_uint(s_d, _OFFSET_ENERGY_P2, 4)
    energy_p1 = en_p1_raw / 100000.0 * 1.66
    energy_p2 = en_p2_raw / 100000.0 * 1.66
    sig_q_pct = sig_q_raw * 100.0 / 255.0

    return DS3Reading(
        serial=serial,
        signal_quality_pct=sig_q_pct,
        vdc1_v=vdc1,
        vdc2_v=vdc2,
        idc1_a=idc1,
        idc2_a=idc2,
        acv_v=acv,
        freq_hz=freq,
        temperature_c=temp,
        timestamp_raw=timestamp,
        energy_p1_wh=energy_p1,
        energy_p2_wh=energy_p2,
    )


def _read_uint(hex_str: str, byte_offset: int, byte_length: int) -> int:
    """Extract an unsigned big-endian integer from a hex string.

    Byte offsets follow the firmware convention: 1 byte == 2 hex chars.
    """
    start = byte_offset * 2
    end = start + byte_length * 2
    if end > len(hex_str):
        raise DS3DecodeError(
            f"frame ends before byte {byte_offset}+{byte_length} "
            f"({len(hex_str)} hex chars available)"
        )
    return int(hex_str[start:end], 16)


def derive_power(previous: DS3Reading | None, current: DS3Reading) -> tuple[float, float, float]:
    """Compute instantaneous (P1, P2, P_total) in watts from two readings.

    Uses the firmware logic from `AAA_DECODE.ino:228-298`:
    - if there is no previous reading or the inverter timestamp wrapped /
      decreased, treat the current cumulative energy as the increment;
    - otherwise compute the increment over the elapsed seconds.

    Returns ``(0.0, 0.0, 0.0)`` when no time has elapsed (Δt = 0).
    """
    if previous is None or current.timestamp_raw < previous.timestamp_raw:
        delta_t = current.timestamp_raw
        delta_e1 = current.energy_p1_wh
        delta_e2 = current.energy_p2_wh
    else:
        delta_t = current.timestamp_raw - previous.timestamp_raw
        delta_e1 = current.energy_p1_wh - previous.energy_p1_wh
        delta_e2 = current.energy_p2_wh - previous.energy_p2_wh

    if delta_t <= 0:
        return 0.0, 0.0, 0.0
    p1 = delta_e1 / delta_t * 3600.0
    p2 = delta_e2 / delta_t * 3600.0
    return p1, p2, p1 + p2
