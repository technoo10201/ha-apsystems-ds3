"""Constants for the APsystems Zigbee (CC2530 direct) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "aps_zigbee"
MANUFACTURER: Final = "APsystems"
MODEL: Final = "DS3"
COORDINATOR_MODEL: Final = "CC2530 ZNP (Kadsol firmware)"

# Config entry data keys
CONF_PORT: Final = "port"
CONF_ECU_ID: Final = "ecu_id"
CONF_INVERTERS: Final = "inverters"
CONF_DTR_RESET: Final = "dtr_reset"

# Inverter dict keys (within CONF_INVERTERS list)
INV_SERIAL: Final = "serial"
INV_ID: Final = "inv_id"
INV_NAME: Final = "name"

# Options keys
CONF_SCAN_INTERVAL: Final = "scan_interval"

# Defaults
DEFAULT_ECU_ID: Final = "D8A3011B9780"
DEFAULT_SCAN_INTERVAL_S: Final = 60
DEFAULT_DTR_RESET: Final = True
MIN_SCAN_INTERVAL_S: Final = 30
MAX_SCAN_INTERVAL_S: Final = 3600

# Robustness knobs
WATCHDOG_INTERVAL_S: Final = 300  # 5 min between coordinator liveness pings
BACKOFF_BASE_S: Final = 1
BACKOFF_MAX_S: Final = 300  # cap per-inverter backoff at 5 min
DEAD_THRESHOLD: Final = 5  # daytime failures before state = "dead"
STARTUP_RETRIES: Final = 3  # async_setup_entry init attempts
STARTUP_RETRY_DELAY_S: Final = 2
RECOVERY_RETRIES: Final = 3  # coordinator recovery attempts before giving up
RECOVERY_BACKOFF_S: Final = (5, 30, 120, 600)  # 5 s, 30 s, 2 min, then 10 min
RECOVERY_IDLE_RETRY_S: Final = 900  # after recovery exhausted, retry every 15 min

# Service names
SERVICE_PAIR_INVERTER: Final = "pair_inverter"
SERVICE_REPOLL: Final = "repoll"
SERVICE_REBOOT_INVERTER: Final = "reboot_inverter"
SERVICE_DISCOVER: Final = "discover"
SERVICE_REBIND_PERSISTENT: Final = "rebind_persistent"
SERVICE_CANCEL_REBIND: Final = "cancel_rebind"

ATTR_SERIAL: Final = "serial"
ATTR_NAME: Final = "name"
ATTR_DURATION_MIN: Final = "duration"
ATTR_PAUSE_S: Final = "pause"

# Persistent re-bind campaign bounds (minutes / seconds)
CAMPAIGN_DURATION_DEFAULT_MIN: Final = 30
CAMPAIGN_DURATION_MAX_MIN: Final = 240
CAMPAIGN_PAUSE_DEFAULT_S: Final = 300
CAMPAIGN_PAUSE_MIN_S: Final = 120
CAMPAIGN_PAUSE_MAX_S: Final = 1800

# Sensor keys exposed by the coordinator data dict (per inverter).
# These are what `SensorEntityDescription.key` maps to.
SENSOR_VDC1: Final = "vdc1_v"
SENSOR_VDC2: Final = "vdc2_v"
SENSOR_IDC1: Final = "idc1_a"
SENSOR_IDC2: Final = "idc2_a"
SENSOR_POWER_P1: Final = "power_p1_w"
SENSOR_POWER_P2: Final = "power_p2_w"
SENSOR_POWER_TOTAL: Final = "power_total_w"
SENSOR_ENERGY_P1: Final = "energy_p1_kwh"
SENSOR_ENERGY_P2: Final = "energy_p2_kwh"
SENSOR_ACV: Final = "acv_v"
SENSOR_FREQUENCY: Final = "freq_hz"
SENSOR_TEMPERATURE: Final = "temperature_c"
SENSOR_SIGNAL_QUALITY: Final = "signal_quality_pct"
SENSOR_MESH_HOPS: Final = "mesh_hops"
