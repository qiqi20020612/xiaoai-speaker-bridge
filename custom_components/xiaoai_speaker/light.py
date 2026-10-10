"""Direct LED control is capability-gated and yields to voice status lights."""

import uuid

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .client import BridgeError
from .entity import XiaoAIEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.coordinator
    if c.info.get("capabilities", {}).get("led"):
        async_add_entities([SpeakerLight(c, "led")])


class SpeakerLight(XiaoAIEntity, LightEntity):
    _attr_translation_key = "led"
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_assumed_state = True

    @property
    def is_on(self):
        return self.coordinator.data.get("led", {}).get("on")

    @property
    def rgb_color(self):
        return self.coordinator.data.get("led", {}).get("rgb")

    @property
    def brightness(self):
        return self.coordinator.data.get("led", {}).get("brightness")

    async def async_turn_on(self, **kwargs):
        try:
            await self.coordinator.client.request(
                "POST",
                "/api/led",
                {
                    "on": True,
                    "rgb": list(kwargs.get("rgb_color", self.rgb_color or (144, 48, 0))),
                    "brightness": kwargs.get("brightness", self.brightness or 255),
                    "request_id": uuid.uuid4().hex,
                },
            )
            await self.coordinator.async_request_refresh()
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None

    async def async_turn_off(self, **kwargs):
        try:
            await self.coordinator.client.request(
                "POST", "/api/led", {"on": False, "request_id": uuid.uuid4().hex}
            )
            await self.coordinator.async_request_refresh()
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None
