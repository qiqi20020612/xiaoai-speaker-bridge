"""Export only an allowlist; omit tokens, addresses, identity and conversation text."""

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry):
    c = entry.runtime_data.coordinator
    state = c.data or {}
    caps = c.info.get("capabilities", {})
    return {
        "integration": DOMAIN,
        "protocol": c.info.get("protocol"),
        "version": c.info.get("version"),
        "model": c.info.get("model"),
        "capabilities": {
            **{
                k: bool(caps.get(k))
                for k in ("announce", "conversation", "voice_hooks", "listener", "infrared")
            },
            "volume": {
                k: bool(caps.get("volume", {}).get(k))
                for k in ("master", "tts", "bluetooth", "headphone", "airplay")
            },
            "playback": {
                k: {
                    "read": bool(caps.get("playback", {}).get(k, {}).get("read")),
                    "actions": [
                        a
                        for a in caps.get("playback", {}).get(k, {}).get("actions", [])
                        if a in ("play", "pause", "stop", "next", "previous", "mute", "unmute")
                    ],
                }
                for k in ("airplay", "bluetooth")
            },
        },
        "available": c.last_update_success,
        "system": {
            k: state.get("system", {}).get(k)
            for k in (
                "uptime",
                "memory_available",
                "memory_total",
                "cpu_load",
                "bridge_rss",
                "bridge_peak_rss",
            )
        },
        "active_source": state.get("active_source"),
        "source_uncertain": state.get("source_uncertain"),
        "airplay": {
            k: state.get("airplay", {}).get(k) for k in ("active", "state", "volume", "raw_volume")
        },
        "last_job_status": c.jobs.last.get("status"),
        "queue_size": c.jobs.queue.qsize(),
    }
