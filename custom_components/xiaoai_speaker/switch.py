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
        [ListenerSwitch(c, "listener")],
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
