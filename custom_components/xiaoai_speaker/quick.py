"""Volatile convenience inputs; send exclusively through the registered services."""

import uuid

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


class QuickControls:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.texts = {"announce": "", "send_text": ""}
        self.settings = {"speak_response": True, "use_context": True}
        self.pending = set()
        self.requests = {}

    def changed(self):
        self.coordinator._job_changed()

    async def send(self, kind, entity_id, context):
        if kind in self.pending:
            raise HomeAssistantError("quick_action_busy")
        previous = self.coordinator.jobs.jobs.get(self.requests.get(kind))
        if previous is not None and not previous.done.is_set():
            raise HomeAssistantError("quick_action_busy")
        text = self.texts[kind]
        if not text.strip():
            raise HomeAssistantError("invalid_text")
        # Claim synchronously before awaiting so rapid clicks cannot duplicate a task.
        self.pending.add(kind)
        request_id = uuid.uuid4().hex
        try:
            payload = {"entity_id": entity_id, "text": text, "request_id": request_id}
            if kind == "send_text":
                payload.update(self.settings)
            await self.coordinator.hass.services.async_call(
                DOMAIN, kind, payload, blocking=True, return_response=True, context=context
            )
            self.requests[kind] = request_id
            if self.texts[kind] == text:
                self.texts[kind] = ""
            self.changed()
        finally:
            self.pending.discard(kind)
