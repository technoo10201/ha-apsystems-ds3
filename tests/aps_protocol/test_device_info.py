"""Tests for SAPI_GET_DEVICE_INFO (0x6700) parsing and check_network.

Fixtures are verbatim ZNP frames captured from the CC2530 dongle and verified
against the SAPI_GET_DEVICE_INFO payload spec.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.aps_zigbee.aps_protocol.frames import (
    CoordinatorDeviceInfo,
    parse_device_info,
)
from custom_components.aps_zigbee.aps_protocol.znp import ZNPError

# ZNP frame — coordinator healthy (DEV_ZB_COORD, ShortAddr=0x0000).
# Payload: Status=0x00 | IEEE LE=FFFF80971B01A3D8 | ShortAddr LE=0000 |
#          DeviceType=0x07 | DeviceState=0x09 | NumAssoc=0x00
BURST_HEALTHY = "FE0E670000FFFF80971B01A3D8000007090011"

# ZNP frame — coordinator in DEV_HOLD (ShortAddr=0xFFFE, DeviceState=0x00).
BURST_DEV_HOLD = "FE0E670000FFFF80971B01A3D8FEFF07000019"


def test_parse_device_info_healthy() -> None:
    info = parse_device_info(BURST_HEALTHY)
    assert info is not None
    assert info.short_addr == "0000"
    assert info.device_state == 0x09
    assert info.network_up is True
    assert info.ieee == "D8A3011B9780FFFF"
    assert info.num_assoc == 0


def test_parse_device_info_dev_hold() -> None:
    info = parse_device_info(BURST_DEV_HOLD)
    assert info is not None
    assert info.short_addr == "FFFE"
    assert info.device_state == 0x00
    assert info.network_up is False


def test_parse_device_info_embedded_in_burst() -> None:
    """Noise before/after, and two 6700 frames — the last one must win."""
    noise = "FE0164010266"  # unrelated frame
    burst = noise + BURST_HEALTHY + BURST_DEV_HOLD
    info = parse_device_info(burst)
    # DEV_HOLD is last → its fields win
    assert info is not None
    assert info.short_addr == "FFFE"
    assert info.network_up is False


def test_parse_device_info_absent_or_truncated_returns_none() -> None:
    # Empty burst
    assert parse_device_info("") is None
    # Burst with no 6700 frame
    assert parse_device_info("FE03448000143D") is None
    # 6700 frame present but payload is cut short (missing FCS)
    assert parse_device_info("FE0E670000FFFF80971B01A3D800") is None


@pytest.mark.asyncio
async def test_check_network_transport_error_returns_none() -> None:
    """A ZNPError during request must yield None without raising."""
    from custom_components.aps_zigbee.aps_protocol import coordinator as coord_mod
    from custom_components.aps_zigbee.aps_protocol.coordinator import check_network

    znp = AsyncMock()
    znp.request = AsyncMock(side_effect=ZNPError("port gone"))

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        result = await check_network(znp)

    assert result is None
