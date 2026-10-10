"""A source-aware native media player; every command reaches its real backend."""

from __future__ import annotations

import uuid

from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature
from homeassistant.components.media_player.const import MediaPlayerState, MediaType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .client import BridgeError
from .entity import XiaoAIEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([XiaoAIMediaPlayer(entry.runtime_data.coordinator)])


class XiaoAIMediaPlayer(XiaoAIEntity, MediaPlayerEntity):
    _attr_name = None

    def __init__(self, coordinator):
        super().__init__(coordinator, "player")
        self._position_updated = dt_util.utcnow()

    @property
    def source(self):
        return self.coordinator.data.get("active_source")

    @property
    def source_list(self):
        return None  # Source display does not claim to switch hardware routing.

    def _backend(self):
        return self.source or "mpd"

    @property
    def supported_features(self):
        source = self._backend()
        if source == "mpd":
            actions = self.coordinator.mpd.commands
        else:
            actions = set(
                self.coordinator.info.get("capabilities", {})
                .get("playback", {})
                .get(source, {})
                .get("actions", [])
            )
        features = MediaPlayerEntityFeature(0)
        for action, flag in (
            ("play", MediaPlayerEntityFeature.PLAY),
            ("pause", MediaPlayerEntityFeature.PAUSE),
            ("stop", MediaPlayerEntityFeature.STOP),
            ("next", MediaPlayerEntityFeature.NEXT_TRACK),
            ("previous", MediaPlayerEntityFeature.PREVIOUS_TRACK),
            ("seekcur", MediaPlayerEntityFeature.SEEK),
            ("clear", MediaPlayerEntityFeature.CLEAR_PLAYLIST),
            ("random", MediaPlayerEntityFeature.SHUFFLE_SET),
        ):
            if action in actions:
                features |= flag
        if source == "mpd" and {"repeat", "single"} <= actions:
            features |= MediaPlayerEntityFeature.REPEAT_SET
        if source == "mpd" and {"addid", "playid", "clear", "moveid", "status"} <= actions:
            features |= MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.MEDIA_ENQUEUE
        try:
            self.coordinator.volume.targets("auto")
        except BridgeError:
            pass
        else:
            features |= MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_STEP
        if "mute" in actions:
            features |= MediaPlayerEntityFeature.VOLUME_MUTE
        return features

    @property
    def state(self):
        source = self._backend()
        state = self.coordinator.data.get(source, {}).get("state", "unknown").lower()
        return {
            "play": MediaPlayerState.PLAYING,
            "playing": MediaPlayerState.PLAYING,
            "pause": MediaPlayerState.PAUSED,
            "paused": MediaPlayerState.PAUSED,
            "stop": MediaPlayerState.IDLE,
            "stopped": MediaPlayerState.IDLE,
            "idle": MediaPlayerState.IDLE,
        }.get(state)

    @property
    def volume_level(self):
        target = {"airplay": "airplay", "bluetooth": "bluetooth", "mpd": "master"}.get(
            self.source, "tts" if self.coordinator.volume.idle_policy == "tts" else "master"
        )
        data = self.coordinator.data
        value = data.get("volumes", {}).get(target)
        if data.get("voice", {}).get("active") or data.get("audio_active"):
            value = self.coordinator.volume.desired.get(
                target, data.get("voice", {}).get("volumes", {}).get(target, value)
            )
        return value / 100 if isinstance(value, (int, float)) else None

    @property
    def is_volume_muted(self):
        return self.coordinator.data.get(self._backend(), {}).get("muted")

    def _song(self):
        return self.coordinator.data.get(self._backend(), {}).get("song", {})

    @property
    def media_title(self):
        return self._song().get("Title") or self._song().get("title") or self._song().get("file")

    @property
    def media_artist(self):
        value = self._song().get("Artist") or self._song().get("artist")
        return ", ".join(value) if isinstance(value, list) else value

    @property
    def media_album_name(self):
        return self._song().get("Album") or self._song().get("album")

    @property
    def media_content_id(self):
        return self._song().get("file")

    @property
    def media_content_type(self):
        return MediaType.MUSIC

    def _status(self):
        return self.coordinator.data.get(self._backend(), {}).get("status", {})

    @property
    def media_duration(self):
        value = self._status().get("duration") or self._song().get("duration")
        try:
            return float(value) if value is not None else None
        except ValueError, TypeError:
            return None

    @property
    def media_position(self):
        value = self._status().get("elapsed")
        try:
            return float(value) if value is not None else None
        except ValueError, TypeError:
            return None

    @property
    def media_position_updated_at(self):
        return self._position_updated

    @property
    def shuffle(self):
        return self._status().get("random") == "1" if self._backend() == "mpd" else None

    @property
    def repeat(self):
        status = self._status()
        return (
            "one"
            if status.get("single") == "1"
            else "all"
            if status.get("repeat") == "1"
            else "off"
        )

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        return {
            "active_source": self.source,
            "source_uncertain": data.get("source_uncertain", False),
            "voice_active": data.get("voice", {}).get("active", False),
            "source_status": {
                s: data.get(s, {}).get("state") for s in ("mpd", "airplay", "bluetooth")
            },
        }

    def _handle_coordinator_update(self):
        self._position_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def _command(self, action, *args):
        try:
            source = self._backend()
            if source == "mpd":
                if action not in self.coordinator.mpd.commands:
                    raise BridgeError("unsupported_playback_action")
                await self.coordinator.mpd.execute(action, *args)
            else:
                caps = (
                    self.coordinator.info.get("capabilities", {})
                    .get("playback", {})
                    .get(source, {})
                )
                if action not in caps.get("actions", []):
                    raise BridgeError("unsupported_playback_action")
                await self.coordinator.client.control(source, action, request_id=uuid.uuid4().hex)
            await self.coordinator.async_request_refresh()
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None

    async def async_media_play(self):
        await self._command("play")

    async def async_media_pause(self):
        await self._command("pause", 1) if self._backend() == "mpd" else await self._command(
            "pause"
        )

    async def async_media_stop(self):
        await self._command("stop")

    async def async_media_next_track(self):
        await self._command("next")

    async def async_media_previous_track(self):
        await self._command("previous")

    async def async_media_seek(self, position):
        if self._backend() != "mpd":
            raise HomeAssistantError("unsupported_playback_action")
        await self._command("seekcur", max(0, float(position)))

    async def async_clear_playlist(self):
        await self._command("clear")

    async def async_set_shuffle(self, shuffle):
        await self._command("random", int(shuffle))

    async def async_set_repeat(self, repeat):
        if repeat not in ("off", "one", "all") or self._backend() != "mpd":
            raise HomeAssistantError("unsupported_playback_action")
        if not {"repeat", "single"} <= self.coordinator.mpd.commands:
            raise HomeAssistantError("unsupported_playback_action")
        await self._command("repeat", int(repeat != "off"))
        await self._command("single", int(repeat == "one"))

    async def async_set_volume_level(self, volume):
        try:
            await self.coordinator.volume.set("auto", float(volume) * 100)
            await self.coordinator.async_request_refresh()
        except BridgeError as err:
            raise HomeAssistantError(err.code) from None

    async def async_volume_up(self):
        if self.volume_level is None:
            raise HomeAssistantError("volume_unknown")
        await self.async_set_volume_level(min(1, self.volume_level + 0.1))

    async def async_volume_down(self):
        if self.volume_level is None:
            raise HomeAssistantError("volume_unknown")
        await self.async_set_volume_level(max(0, self.volume_level - 0.1))

    async def async_mute_volume(self, mute):
        await self._command("mute" if mute else "unmute")

    async def async_play_media(self, media_type, media_id, **kwargs):
        if self._backend() != "mpd":
            raise HomeAssistantError("unsupported_playback_action")
        try:
            enqueue = kwargs.get("enqueue", "replace")
            enqueue = getattr(enqueue, "value", enqueue)
            await self.coordinator.mpd.play_media(media_id, enqueue)
            await self.coordinator.async_request_refresh()
        except (BridgeError, ValueError) as err:
            raise HomeAssistantError(str(err)) from None
