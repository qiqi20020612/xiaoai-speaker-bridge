"""Native inputs for non-sensitive instructions: HA may record their states/events."""

from homeassistant.components.text import TextEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import XiaoAIEntity, register_capability_entities


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.coordinator
    register_capability_entities(
        entry, async_add_entities, [InstructionText(c, kind) for kind in ("announce", "send_text")]
    )


class InstructionText(XiaoAIEntity, TextEntity):
    _attr_native_min = 0
    _attr_native_max = 255  # Native HA state length; longer input uses the existing service.
    _attr_icon = "mdi:form-textbox"

    def __init__(self, coordinator, kind):
        super().__init__(coordinator, f"{kind}_text")
        self.kind = kind
        self.required_capability = ("announce" if kind == "announce" else "conversation",)
        self._attr_translation_key = f"{kind}_text"

    @property
    def native_value(self):
        return self.coordinator.quick.texts[self.kind]

    @property
    def extra_state_attributes(self):
        job = self.coordinator.jobs.jobs.get(self.coordinator.quick.requests.get(self.kind))
        return {
            "status": job.status if job is not None else "idle",
            "request_id": job.request_id if job is not None else None,
            "error": job.result.get("error") if job is not None else None,
        }

    async def async_set_value(self, value):
        self.require_available()
        if not isinstance(value, str) or not value.strip() or len(value) > 255 or "\0" in value:
            raise HomeAssistantError("invalid_text")
        await self.coordinator.quick.send(self.kind, self.entity_id, self._context, text=value)
        self.async_write_ha_state()
