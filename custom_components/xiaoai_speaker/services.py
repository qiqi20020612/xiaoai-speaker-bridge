"""Explicit-device services with optional response data and bounded inputs."""

import uuid

import probatio as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .client import BridgeError
from .const import DOMAIN, MAX_TEXT, TARGETS


def resolve(hass, data):
    ids = set()
    if data.get("device_id"):
        device = dr.async_get(hass).async_get(data["device_id"])
        if device:
            ids.update(device.config_entries)
    if data.get("entity_id"):
        entity = er.async_get(hass).async_get(data["entity_id"])
        if entity and entity.config_entry_id:
            ids.add(entity.config_entry_id)
    candidates = [value for key, value in hass.data.get(DOMAIN, {}).items() if key in ids]
    if len(candidates) != 1:
        raise ServiceValidationError("Select exactly one XiaoAI speaker device or entity")
    return candidates[0]


DEVICE = {vol.Optional("device_id"): str, vol.Optional("entity_id"): str}


def register_services(hass):
    if hass.services.has_service(DOMAIN, "announce"):
        return

    async def handle(call: ServiceCall):
        coordinator = resolve(hass, call.data)
        payload = {
            key: value
            for key, value in call.data.items()
            if key not in ("device_id", "entity_id", "wait")
        }
        try:
            if not coordinator.last_update_success and call.service not in (
                "cancel_job",
                "get_job",
                "set_idle_volume_policy",
            ):
                raise BridgeError("device_offline")
            if call.service in ("announce", "send_text"):
                caps = coordinator.info.get("capabilities", {})
                capability = "announce" if call.service == "announce" else "conversation"
                if not caps.get(capability) or coordinator.data.get("state_stale"):
                    raise BridgeError("capability_unavailable")
                if (
                    call.service == "send_text"
                    and payload.get("speak_response", True)
                    and not caps.get("announce")
                ):
                    raise BridgeError("tts_unavailable")
                if (
                    caps.get("announcement_ducking", False)
                    and coordinator.entry.options.get("announce_ducking")
                    and (call.service == "announce" or payload.get("speak_response", True))
                ):
                    payload["duck_volume"] = coordinator.entry.options.get(
                        "announce_duck_volume", 10
                    )
                job = await coordinator.jobs.submit(call.service, payload, call.context.id)
                result = (
                    await coordinator.jobs.wait(job)
                    if call.data.get("wait")
                    else {"request_id": job.request_id, "status": job.status}
                )
            elif call.service == "cancel_job":
                result = await coordinator.jobs.cancel(call.data["request_id"])
            elif call.service == "get_job":
                job = coordinator.jobs.jobs.get(call.data["request_id"])
                if job is None:
                    raise BridgeError("job_not_found")
                result = {"request_id": job.request_id, "status": job.status, **job.result}
            elif call.service == "set_volume":
                result = await coordinator.volume.set(call.data["target"], call.data["level"])
                await coordinator.async_request_refresh()
            elif call.service in ("volume_up", "volume_down"):
                targets = coordinator.volume.targets(call.data["target"])
                result = {"targets": targets}
                for target in targets:
                    current = coordinator.volume.desired.get(
                        target, coordinator.data.get("volumes", {}).get(target)
                    )
                    if current is None:
                        raise BridgeError("volume_unknown")
                    delta = call.data["step"] * (1 if call.service == "volume_up" else -1)
                    await coordinator.volume.set(target, max(0, min(100, current + delta)))
            elif call.service == "set_idle_volume_policy":
                hass.config_entries.async_update_entry(
                    coordinator.entry,
                    options={**coordinator.entry.options, "idle_policy": call.data["policy"]},
                )
                result = {"policy": call.data["policy"]}
            elif call.service == "get_queue":
                result = {"queue": await coordinator.mpd.queue()}
            elif call.service == "send_ir":
                result = await coordinator.client.request(
                    "POST", "/api/ir", {**payload, "request_id": uuid.uuid4().hex}
                )
            else:
                raise BridgeError("unsupported_action")
            return result if call.return_response else None
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None

    text = {
        **DEVICE,
        vol.Required("text"): vol.All(str, vol.Length(min=1, max=MAX_TEXT)),
        vol.Optional("request_id"): str,
        vol.Optional("wait", default=False): bool,
        vol.Optional("priority", default="normal"): vol.In(["normal", "urgent"]),
    }
    definitions = {
        "announce": {
            **text,
            vol.Optional("volume"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        },
        "send_text": {
            **text,
            vol.Optional("speak_response", default=True): bool,
            vol.Optional("use_context", default=True): bool,
            vol.Optional("agent_id"): str,
        },
        "cancel_job": {**DEVICE, vol.Required("request_id"): str},
        "get_job": {**DEVICE, vol.Required("request_id"): str},
        "set_volume": {
            **DEVICE,
            vol.Optional("target", default="auto"): vol.In(TARGETS),
            vol.Required("level"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        },
        "volume_up": {
            **DEVICE,
            vol.Optional("target", default="auto"): vol.In(TARGETS),
            vol.Optional("step", default=10): vol.All(vol.Coerce(float), vol.Range(min=1, max=100)),
        },
        "volume_down": {
            **DEVICE,
            vol.Optional("target", default="auto"): vol.In(TARGETS),
            vol.Optional("step", default=10): vol.All(vol.Coerce(float), vol.Range(min=1, max=100)),
        },
        "set_idle_volume_policy": {
            **DEVICE,
            vol.Required("policy"): vol.In(["master", "tts", "master_tts"]),
        },
        "get_queue": {**DEVICE},
        "send_ir": {
            **DEVICE,
            vol.Required("code"): vol.All(str, vol.Length(max=8192)),
            vol.Optional("carrier", default=38000): vol.All(
                vol.Coerce(int), vol.Range(min=20000, max=60000)
            ),
        },
    }
    for name, fields in definitions.items():
        hass.services.async_register(
            DOMAIN,
            name,
            handle,
            schema=vol.Schema(fields),
            supports_response=SupportsResponse.OPTIONAL,
        )
