"""Bounded management actions, with no arbitrary service names or shell."""

import uuid

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .client import BridgeError
from .entity import XiaoAIEntity, register_capability_entities


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.coordinator
    register_capability_entities(
        entry,
        async_add_entities,
        [ManagementButton(c, key) for key in ("refresh", "reset_context", "restart_bridge")],
    )


class ManagementButton(XiaoAIEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, key):
        # Keep the old identity and any user-defined Entity ID/disabled choice.
        super().__init__(coordinator, "restart_listener" if key == "restart_bridge" else key)
        self.key = key
        self._attr_translation_key = key
        if key == "restart_bridge":
            self._attr_device_class = ButtonDeviceClass.RESTART
        self.required_capability = {
            "reset_context": ("conversation",),
        }.get(key, ())
        self._attr_entity_registry_enabled_default = key == "reset_context"

    async def async_press(self):
        self.require_available()
        try:
            if self.key == "refresh":
                await self.coordinator.async_request_refresh()
            elif self.key == "restart_bridge":
                # The endpoint acknowledges scheduling, before its HTTP server exits.
                await self.coordinator.client.request("POST", "/api/bridge/restart", {}, timeout=3)
            else:
                await self.coordinator.client.request(
                    "POST", "/api/conversation/reset", {"request_id": uuid.uuid4().hex}
                )
                await self.coordinator.async_request_refresh()
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None
