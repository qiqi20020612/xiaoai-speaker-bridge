"""Volatile convenience inputs; send exclusively through the registered services."""

import logging
import uuid

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class QuickControls:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.texts = {"announce": "", "send_text": ""}
        self.pending = set()
        self.requests = {}

    def changed(self):
        self.coordinator._job_changed()

    async def send(self, kind, entity_id, context, *, text=None):
        if kind in self.pending:
            raise HomeAssistantError("quick_action_busy")
        previous = self.coordinator.jobs.jobs.get(self.requests.get(kind))
        if previous is not None and not previous.done.is_set():
            raise HomeAssistantError("quick_action_busy")
        text = self.texts[kind] if text is None else text
        if not isinstance(text, str) or not text.strip():
            raise HomeAssistantError("invalid_text")
        # Claim synchronously before awaiting so rapid submissions cannot duplicate a task.
        self.pending.add(kind)
        request_id = uuid.uuid4().hex
        try:
            payload = {"entity_id": entity_id, "text": text, "request_id": request_id}
            await self.coordinator.hass.services.async_call(
                DOMAIN, kind, payload, blocking=True, return_response=True, context=context
            )
        except HomeAssistantError:
            # Preserve the unqueued input for retry; no submission is logged.
            self.texts[kind] = text
            self.changed()
            raise
        else:
            self.requests[kind] = request_id
            self.texts[kind] = ""
            self.changed()
            hass = self.coordinator.hass
            if hass.services.has_service("logbook", "log"):
                state = hass.states.get(entity_id)
                try:
                    await hass.services.async_call(
                        "logbook",
                        "log",
                        {
                            "name": state.name if state is not None else entity_id,
                            "message": text,
                            "entity_id": entity_id,
                            "domain": "text",
                        },
                        blocking=True,
                        context=context,
                    )
                except HomeAssistantError:
                    # The task is already queued. Reporting submission failure
                    # here could prompt a duplicate instruction.
                    _LOGGER.warning("Task %s queued, but its Logbook entry failed", request_id)
        finally:
            self.pending.discard(kind)
