"""State and resource sensors; no fabricated values or dialogue in diagnostics."""

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory

from .entity import XiaoAIEntity

SENSORS = {
    "memory_available": ("kB", "mdi:memory"),
    "cpu_load": (None, "mdi:chip"),
    "bridge_rss": ("kB", "mdi:memory"),
    "airplay_volume": ("%", "mdi:volume-high"),
}


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.coordinator
    async_add_entities([StateSensor(c, key) for key in SENSORS])


class StateSensor(XiaoAIEntity, SensorEntity):
    @property
    def extra_state_attributes(self):
        if self.key == "airplay_volume":
            return {
                "raw_volume": self.coordinator.data.get("airplay", {}).get("raw_volume"),
                "raw_volume_unit": "dB",
            }
        return None

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self.key = key
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement, self._attr_icon = SENSORS[key]
        if key != "airplay_volume":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        data = self.coordinator.data
        if self.key == "airplay_volume":
            return data.get("airplay", {}).get("volume")
        return data.get("system", {}).get(self.key)
