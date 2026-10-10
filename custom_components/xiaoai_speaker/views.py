"""Authenticated HA-local rule resolution; LX06 still executes conversations."""

import re

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.http import KEY_HASS

from .client import BridgeError
from .const import DOMAIN, MAX_TEXT


def get_coordinator(request, device_uid):
    hass = request.app[KEY_HASS]
    return next(
        (c for c in hass.data.get(DOMAIN, {}).values() if c.entry.unique_id == device_uid), None
    )


class VoiceGuardView(HomeAssistantView):
    url = "/api/xiaoai_speaker/{device_uid}/voice_guard"
    name = "api:xiaoai_speaker:voice_guard"
    requires_auth = True

    async def post(self, request, device_uid):
        if request.content_length is None or request.content_length > 8192:
            return self.json({"error": "invalid_request"}, status_code=400)
        coordinator = get_coordinator(request, device_uid)
        if coordinator is None:
            return self.json({"error": "device_not_found"}, status_code=404)
        try:
            payload = await request.json()
            request_id = payload.get("request_id")
            text = payload.get("text")
            if not isinstance(request_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", request_id
            ):
                raise BridgeError("invalid_request_id")
            if text is not None and (
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode()) > MAX_TEXT
                or "\0" in text
            ):
                raise BridgeError("invalid_text")
            coordinator.jobs.voice_guard(request_id, text)
            return self.json({"accepted": True})
        except ValueError, TypeError, AttributeError:
            return self.json({"error": "invalid_request"}, status_code=400)
        except BridgeError as err:
            return self.json({"error": err.code}, status_code=409)


class VolumeResolveView(HomeAssistantView):
    url = "/api/xiaoai_speaker/{device_uid}/volume_plan"
    name = "api:xiaoai_speaker:volume_plan"
    requires_auth = True

    async def post(self, request, device_uid):
        if request.content_length is None or request.content_length > 8192:
            return self.json({"error": "invalid_request"}, status_code=400)
        coordinator = get_coordinator(request, device_uid)
        if coordinator is None:
            return self.json({"error": "device_not_found"}, status_code=404)
        try:
            payload = await request.json()
            text = payload.get("text", "")
            if not isinstance(text, str) or len(text.encode()) > MAX_TEXT:
                raise BridgeError("invalid_text")
            if payload.get("commit"):
                # Only persist actual success acknowledgements, never preemptively save a plan.
                plan = payload.get("plan", [])
                if not isinstance(plan, list) or len(plan) > 5:
                    raise BridgeError("invalid_volume")
                for item in plan:
                    if (
                        item.get("target") not in coordinator.volume.caps.get("volume", {})
                        or not isinstance(item.get("level"), (int, float))
                        or isinstance(item.get("level"), bool)
                        or not 0 <= item["level"] <= 100
                    ):
                        raise BridgeError("invalid_volume")
                await coordinator.volume.commit(plan)
                return self.json({"committed": True})
            result = coordinator.volume.plan(
                text, payload.get("snapshot"), payload.get("local_intent")
            )
            return self.json(result)
        except ValueError, TypeError, AttributeError:
            return self.json({"error": "invalid_request"}, status_code=400)
        except BridgeError as err:
            return self.json({"error": err.code}, status_code=409)


def register_views(hass):
    if not hass.data.setdefault(f"{DOMAIN}_views", False):
        hass.http.register_view(VolumeResolveView())
        hass.http.register_view(VoiceGuardView())
        hass.data[f"{DOMAIN}_views"] = True
