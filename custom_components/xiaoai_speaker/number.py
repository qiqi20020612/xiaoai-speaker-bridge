"""Independent channel sliders, advertised only for real writable controls."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError

from .client import BridgeError
from .entity import XiaoAIEntity, register_capability_entities


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data.coordinator
    register_capability_entities(
        entry,
        async_add_entities,
        [
            VolumeNumber(coordinator, key)
            for key in ("master", "tts", "bluetooth", "headphone", "airplay")
        ],
    )


class VolumeNumber(XiaoAIEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:volume-high"

    def __init__(self, coordinator, channel):
        super().__init__(coordinator, f"volume_{channel}")
        self.channel = channel
        self.required_capability = ("volume", channel)
        self._attr_translation_key = f"volume_{channel}"
        self._attr_entity_registry_enabled_default = channel in ("master", "tts")

    @property
    def available(self):
        return super().available and bool(
            self.coordinator.info.get("capabilities", {}).get("volume", {}).get(self.channel)
        )

    @property
    def native_value(self):
        data = self.coordinator.data
        value = data.get("volumes", {}).get(self.channel)
        if data.get("voice", {}).get("active") or data.get("audio_active"):
            value = self.coordinator.volume.desired.get(
                self.channel, data.get("voice", {}).get("volumes", {}).get(self.channel, value)
            )
        return value

    async def async_set_native_value(self, value):
        self.require_available()
        try:
            await self.coordinator.volume.set(self.channel, value)
            await self.coordinator.async_request_refresh()
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None
