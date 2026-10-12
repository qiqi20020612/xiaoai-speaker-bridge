"""Online, voice, playback and source confidence diagnostics."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .entity import XiaoAIEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.coordinator
    async_add_entities(
        [
            StateBinary(c, key)
            for key in ("online", "voice_active", "airplay_active", "source_uncertain")
        ]
    )


class StateBinary(XiaoAIEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self.key = key
        self._attr_translation_key = key
        self._attr_entity_registry_enabled_default = key != "source_uncertain"
        if key in ("voice_active", "airplay_active"):
            self._attr_entity_category = None
        if key == "online":
            self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def available(self):
        return True if self.key == "online" else super().available

    @property
    def is_on(self):
        if self.key == "online":
            return self.coordinator.last_update_success
        if self.key == "voice_active":
            return self.coordinator.data.get("voice", {}).get("active")
        if self.key == "airplay_active":
            return self.coordinator.data.get("airplay", {}).get("active")
        return self.coordinator.data.get("source_uncertain")
