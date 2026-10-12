"""Persistent idle volume policy belongs to HA."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory

from .const import IDLE_POLICIES
from .entity import XiaoAIEntity, register_capability_entities


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.coordinator
    register_capability_entities(
        entry, async_add_entities, [IdlePolicySelect(c, "idle_policy"), AgentSelect(c, "agent")]
    )


class IdlePolicySelect(XiaoAIEntity, SelectEntity):
    _attr_translation_key = "idle_policy"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(IDLE_POLICIES)

    @property
    def current_option(self):
        return self.coordinator.volume.idle_policy

    async def async_select_option(self, option):
        self.hass.config_entries.async_update_entry(
            self.coordinator.entry,
            options={**self.coordinator.entry.options, "idle_policy": option},
        )


class AgentSelect(XiaoAIEntity, SelectEntity):
    required_capability = ("conversation",)
    _attr_translation_key = "agent"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    @property
    def options(self):
        return ["device_default", *sorted(self.hass.states.async_entity_ids("conversation"))]

    @property
    def current_option(self):
        return self.coordinator.entry.options.get("agent_id", "device_default")

    async def async_select_option(self, option):
        import uuid

        from homeassistant.exceptions import HomeAssistantError

        from .client import BridgeError

        self.require_available()
        if option not in self.options:
            raise HomeAssistantError("invalid_agent")
        try:
            await self.coordinator.client.request(
                "POST",
                "/api/conversation/agent",
                {
                    "agent_id": None if option == "device_default" else option,
                    "request_id": uuid.uuid4().hex,
                },
            )
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None
        self.hass.config_entries.async_update_entry(
            self.coordinator.entry, options={**self.coordinator.entry.options, "agent_id": option}
        )
