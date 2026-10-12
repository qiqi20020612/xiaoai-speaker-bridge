"""Listening is a microphone/recognition service, separate from speaker mute."""

import uuid

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .client import BridgeError
from .entity import XiaoAIEntity, register_capability_entities


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.coordinator
    register_capability_entities(
        entry,
        async_add_entities,
        [ListenerSwitch(c, "listener")]
        + [ConversationSetting(c, key) for key in ("speak_response", "use_context")],
    )


class ListenerSwitch(XiaoAIEntity, SwitchEntity):
    required_capability = ("listener",)
    _attr_translation_key = "listener"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:microphone"

    @property
    def is_on(self):
        return self.coordinator.data.get("listener", {}).get("running")

    async def _set(self, running):
        self.require_available()
        try:
            await self.coordinator.client.request(
                "POST",
                "/api/listener",
                {"action": "start" if running else "stop", "request_id": uuid.uuid4().hex},
            )
            await self.coordinator.async_request_refresh()
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class ConversationSetting(XiaoAIEntity, SwitchEntity):
    required_capability = ("conversation",)
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self.key = key
        self._attr_translation_key = key

    @property
    def is_on(self):
        return self.coordinator.quick.settings[self.key]

    async def _set(self, value):
        self.require_available()
        self.coordinator.quick.settings[self.key] = value
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)
