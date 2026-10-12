"""State and resource sensors; no fabricated values or dialogue in diagnostics."""

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from .entity import XiaoAIEntity

SENSORS = {
    "uptime": ("s", "mdi:timer-outline"),
    "memory_available": ("kB", "mdi:memory"),
    "memory_total": ("kB", "mdi:memory"),
    "cpu_load": (None, "mdi:chip"),
    "bridge_rss": ("kB", "mdi:memory"),
    "bridge_peak_rss": ("kB", "mdi:memory"),
    "active_source": (None, "mdi:music"),
    "job_status": (None, "mdi:progress-clock"),
    "queue_size": (None, "mdi:playlist-play"),
    "airplay_volume": ("%", "mdi:volume-high"),
    "airplay_raw_volume": ("dB", "mdi:volume-high"),
    "conversation_remaining": ("s", "mdi:chat-outline"),
}
LEGACY_SENSORS = frozenset({"bridge_peak_rss", "memory_total", "airplay_raw_volume"})


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.coordinator
    registry = er.async_get(hass)
    async_add_entities(
        [
            StateSensor(c, key)
            for key in SENSORS
            if key not in LEGACY_SENSORS
            or registry.async_get_entity_id("sensor", "xiaoai_speaker", f"{entry.unique_id}_{key}")
        ]
    )


class StateSensor(XiaoAIEntity, SensorEntity):
    @property
    def extra_state_attributes(self):
        if self.key == "job_status":
            return {"request_id": self.coordinator.jobs.last.get("request_id")}
        return None

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self.key = key
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement, self._attr_icon = SENSORS[key]
        if key not in ("active_source", "airplay_volume"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False
        if key in (
            "memory_available",
            "memory_total",
            "bridge_rss",
            "bridge_peak_rss",
            "cpu_load",
            "queue_size",
        ):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        data = self.coordinator.data
        if self.key == "active_source":
            return data.get("active_source") or "idle"
        if self.key in ("job_status", "queue_size"):
            return data.get("job", {}).get("status" if self.key == "job_status" else "queue_size")
        if self.key.startswith("airplay_"):
            return data.get("airplay", {}).get(
                "volume" if self.key == "airplay_volume" else "raw_volume"
            )
        if self.key == "conversation_remaining":
            return data.get("conversation", {}).get("remaining")
        return data.get("system", {}).get(self.key)
