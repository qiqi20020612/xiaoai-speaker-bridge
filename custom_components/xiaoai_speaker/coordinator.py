"""Aggregate protocol state and own event subscriptions inside HA."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AuthenticationError, BridgeError
from .const import DEFAULT_POLL, DOMAIN, SOURCE_PRIORITY
from .jobs import JobManager
from .quick import QuickControls
from .volume import VolumeManager, choose_source

_LOGGER = logging.getLogger(__name__)


class XiaoAICoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client, mpd) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=entry.options.get("poll_interval", DEFAULT_POLL)),
        )
        self.entry = entry
        self.client = client
        self.mpd = mpd
        self.info: dict = {}
        self.volume = VolumeManager(hass, entry, client)
        self.jobs = JobManager(hass, client, self._job_changed)
        self.quick = QuickControls(self)
        self._events_task = None
        self._sequence = 0
        self._closed = False
        self._synced_instance = None
        self._synced_agent = None

    def _job_changed(self) -> None:
        if self.data:
            self.async_set_updated_data({**self.data, "job": self.jobs.last})

    async def _async_setup(self) -> None:
        try:
            self.info = await self.client.info()
            if self.info["device_id"] != self.entry.unique_id:
                raise BridgeError("device_identity_changed")
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except BridgeError as err:
            raise UpdateFailed(err.code) from err
        self.volume.caps = self.info.get("capabilities", {})
        await self.volume.load()

    async def _async_update_data(self) -> dict:
        try:
            state = await self.client.state()
            if state.get("device_id", self.entry.unique_id) != self.entry.unique_id:
                raise BridgeError("device_identity_changed")
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except BridgeError as err:
            raise UpdateFailed(err.code) from err
        if self.mpd:
            try:
                if not self.mpd.commands:
                    await self.mpd.discover()
                state["mpd"] = await self.mpd.snapshot()
            except BridgeError:
                self.mpd.commands.clear()
                state["mpd"] = {"online": False, "state": "unknown", "actions": []}
        priority = self.entry.options.get("source_priority", list(SOURCE_PRIORITY))
        decision = choose_source(state, priority)
        state["active_source"] = decision.source
        state["source_uncertain"] = decision.uncertain
        state["job"] = self.jobs.last
        self.volume.state = state
        if "capabilities" in state:
            self.info["capabilities"] = state["capabilities"]
            self.volume.caps = state["capabilities"]
        instance = state.get("instance_id")
        if instance and instance != self._synced_instance:
            all_synced = True
            for target, level in list(self.volume.desired.items()):
                if not self.volume.caps.get("volume", {}).get(target):
                    continue
                try:
                    await self.client.volume(target, level, uuid.uuid4().hex)
                except BridgeError:
                    all_synced = False
            if all_synced:
                self._synced_instance = instance
        elif (
            instance
            and not state.get("voice", {}).get("active")
            and not state.get("audio_active")
            and not self.jobs.executing
            and not state.get("state_stale")
        ):
            changed = []
            for target, level in state.get("volumes", {}).items():
                if (
                    self.volume.caps.get("volume", {}).get(target)
                    and isinstance(level, (int, float))
                    and 0 <= level <= 100
                    and self.volume.desired.get(target) != level
                ):
                    changed.append({"target": target, "level": level})
            if changed:
                await self.volume.commit(changed)
        agent = self.entry.options.get("agent_id", "device_default")
        if (
            instance
            and (instance, agent) != self._synced_agent
            and self.volume.caps.get("conversation")
        ):
            try:
                await self.client.request(
                    "POST",
                    "/api/conversation/agent",
                    {
                        "agent_id": None if agent == "device_default" else agent,
                        "request_id": uuid.uuid4().hex,
                    },
                )
                self._synced_agent = (instance, agent)
            except BridgeError:
                pass
        # Lightweight device snapshots only; HA retains policy and durable desired values.
        try:
            await self.client.request(
                "POST",
                "/api/policy",
                {
                    "source": decision.source,
                    "uncertain": decision.uncertain,
                    "idle_policy": self.volume.idle_policy,
                },
            )
        except BridgeError:
            pass
        return state

    def start(self) -> None:
        self.jobs.start()
        self._events_task = self.hass.async_create_background_task(self._events(), "XiaoAI events")

    async def _events(self) -> None:
        delay = 1
        while not self._closed:
            try:
                event = await self.client.events(self._sequence)
                sequence = event.get("sequence", 0)
                if event.get("reset") or sequence != self._sequence:
                    self._sequence = sequence
                    await self.async_request_refresh()
                delay = 1
            except BridgeError:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def close(self) -> None:
        self._closed = True
        if self._events_task:
            self._events_task.cancel()
            await asyncio.gather(self._events_task, return_exceptions=True)
        await self.jobs.close()
