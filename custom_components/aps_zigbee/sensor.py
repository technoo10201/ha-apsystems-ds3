"""Sensor platform for the APsystems Zigbee integration.

Every paired inverter is exposed as a device with up to 13 entities (two DC
chains x {voltage, current, power, energy} plus AC voltage, frequency,
temperature, signal quality, total power). Each entity reads its value out of
the coordinator's per-inverter dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    INV_ID,
    INV_NAME,
    INV_SERIAL,
    MANUFACTURER,
    MODEL,
    SENSOR_ACV,
    SENSOR_ENERGY_P1,
    SENSOR_ENERGY_P2,
    SENSOR_FREQUENCY,
    SENSOR_IDC1,
    SENSOR_IDC2,
    SENSOR_MESH_HOPS,
    SENSOR_POWER_P1,
    SENSOR_POWER_P2,
    SENSOR_POWER_TOTAL,
    SENSOR_SIGNAL_QUALITY,
    SENSOR_TEMPERATURE,
    SENSOR_VDC1,
    SENSOR_VDC2,
)
from .coordinator import APSDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


@dataclass(frozen=True, kw_only=True)
class APSSensorDescription(SensorEntityDescription):
    """Sensor description bundling the coordinator-data key."""

    data_key: str


SENSORS: tuple[APSSensorDescription, ...] = (
    APSSensorDescription(
        key=SENSOR_VDC1,
        data_key=SENSOR_VDC1,
        translation_key="vdc1",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    APSSensorDescription(
        key=SENSOR_VDC2,
        data_key=SENSOR_VDC2,
        translation_key="vdc2",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    APSSensorDescription(
        key=SENSOR_IDC1,
        data_key=SENSOR_IDC1,
        translation_key="idc1",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    APSSensorDescription(
        key=SENSOR_IDC2,
        data_key=SENSOR_IDC2,
        translation_key="idc2",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
    ),
    APSSensorDescription(
        key=SENSOR_POWER_P1,
        data_key=SENSOR_POWER_P1,
        translation_key="power_p1",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
    ),
    APSSensorDescription(
        key=SENSOR_POWER_P2,
        data_key=SENSOR_POWER_P2,
        translation_key="power_p2",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
    ),
    APSSensorDescription(
        key=SENSOR_POWER_TOTAL,
        data_key=SENSOR_POWER_TOTAL,
        translation_key="power_total",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
    ),
    APSSensorDescription(
        key=SENSOR_ENERGY_P1,
        data_key=SENSOR_ENERGY_P1,
        translation_key="energy_p1",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
    APSSensorDescription(
        key=SENSOR_ENERGY_P2,
        data_key=SENSOR_ENERGY_P2,
        translation_key="energy_p2",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
    APSSensorDescription(
        key=SENSOR_ACV,
        data_key=SENSOR_ACV,
        translation_key="acv",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    APSSensorDescription(
        key=SENSOR_FREQUENCY,
        data_key=SENSOR_FREQUENCY,
        translation_key="frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
    ),
    APSSensorDescription(
        key=SENSOR_TEMPERATURE,
        data_key=SENSOR_TEMPERATURE,
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    APSSensorDescription(
        key=SENSOR_SIGNAL_QUALITY,
        data_key=SENSOR_SIGNAL_QUALITY,
        translation_key="signal_quality",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:wifi",
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    APSSensorDescription(
        key=SENSOR_MESH_HOPS,
        data_key=SENSOR_MESH_HOPS,
        translation_key="mesh_hops",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:transit-connection-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Spawn one sensor per (inverter, metric) pair."""
    coordinator: APSDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[APSSensor] = []
    for inv in coordinator.inverters:
        for desc in SENSORS:
            entities.append(APSSensor(coordinator, inv, desc))
    async_add_entities(entities)


class APSSensor(CoordinatorEntity[APSDataUpdateCoordinator], SensorEntity):
    """One metric (voltage, power…) for one paired inverter."""

    _attr_has_entity_name = True
    entity_description: APSSensorDescription

    def __init__(
        self,
        coordinator: APSDataUpdateCoordinator,
        inverter: dict[str, str],
        description: APSSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._serial = inverter[INV_SERIAL]
        self._attr_unique_id = f"{self._serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=inverter.get(INV_NAME) or f"APS DS3 {self._serial[-4:]}",
            serial_number=self._serial,
            via_device=(DOMAIN, f"coordinator:{coordinator.znp.port}"),
            sw_version=inverter.get(INV_ID),
        )

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = (self.coordinator.data or {}).get(self._serial)
        return bool(data and data.get("available", False))

    @property
    def native_value(self) -> Any:
        data = (self.coordinator.data or {}).get(self._serial)
        if data is None:
            return None
        return data.get(self.entity_description.data_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        # Only the headline metric (total power) carries the runtime state so
        # the dashboard isn't littered with the same trio of attributes on
        # every entity. The mesh-hops diagnostic carries the relay path.
        data = (self.coordinator.data or {}).get(self._serial)
        if data is None:
            return None
        if self.entity_description.key == SENSOR_MESH_HOPS:
            return {"route": data.get("route")}
        if self.entity_description.key != SENSOR_POWER_TOTAL:
            return None
        return {
            "state": data.get("state"),
            "last_seen": data.get("last_seen"),
            "consecutive_failures": data.get("consecutive_failures"),
        }
