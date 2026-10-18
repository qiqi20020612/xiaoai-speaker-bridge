"""HA owns volume routing, language interpretation and persistent desired values."""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from dataclasses import dataclass

from homeassistant.helpers.storage import Store

from .client import BridgeError
from .const import DOMAIN, SOURCE_PRIORITY


@dataclass(frozen=True)
class SourceDecision:
    source: str | None
    uncertain: bool = False


def choose_source(state: dict, priority: tuple | list = SOURCE_PRIORITY) -> SourceDecision:
    voice = state.get("voice", {})
    if voice.get("active"):
        observed = voice.get("observed_sources")
        if isinstance(observed, dict) and set(observed) == set(SOURCE_PRIORITY):
            # Resolve only the frozen pre-attenuation observations, using the
            # configured HA priority. Later media state cannot change this route.
            return choose_source(
                {
                    **{s: {"active": observed[s]} for s in SOURCE_PRIORITY},
                    "state_stale": state.get("state_stale", False),
                },
                priority,
            )
        active = voice.get("active_sources", [])
        source = next((s for s in priority if s in active), voice.get("source"))
        return SourceDecision(
            source,
            bool(
                state.get("state_stale")
                or len(active) > 1
                or voice.get("uncertain", source is None)
            ),
        )
    active = []
    unknown = []
    for source in priority:
        value = state.get(source, {})
        activity = value.get("active")
        playback = str(value.get("state", "unknown")).lower()
        if activity is True or (not isinstance(activity, bool) and playback in ("play", "playing")):
            active.append(source)
        elif not isinstance(activity, bool) and playback not in (
            "pause",
            "paused",
            "stop",
            "stopped",
            "idle",
            "off",
        ):
            unknown.append(source)
    # Confidence concerns the selected source. An unobserved lower-priority
    # source cannot change that decision; multiple confirmed sources still do.
    unresolved = (
        any(priority.index(s) < priority.index(active[0]) for s in unknown)
        if active
        else bool(unknown)
    )
    return SourceDecision(
        active[0] if active else None,
        bool(state.get("state_stale") or len(active) > 1 or unresolved),
    )


def resolve_targets(target: str, state: dict, caps: dict, source: str | None = None) -> list[str]:
    source = source if source is not None else state.get("active_source")
    if target == "auto":
        target = {"mpd": "master", "bluetooth": "bluetooth", "airplay": "airplay"}.get(
            source, "master"
        )
    targets = [target]
    volumes = caps.get("volume", {})
    if target == "all":
        targets = [v for v in ("master", "tts", "bluetooth", "headphone") if volumes.get(v)]
        if source == "airplay":
            # Never attenuate AirPlay both in software and again through mysoftvol.
            targets = [v for v in targets if v != "master"] + ["airplay"]
    if not targets or any(not volumes.get(t) for t in targets):
        raise BridgeError("unsupported_volume_target")
    return targets


# Deliberately narrow, auditable volume phrases; normal conversation stays on LX06.
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    if value in ("一百", "百"):
        return 100
    if "十" in value:
        left, right = value.split("十", 1)
        return _CN_DIGITS.get(left, 1) * 10 + _CN_DIGITS.get(right, 0)
    return _CN_DIGITS.get(value, -1)


def volume_intent(text: str) -> dict | None:
    compact = re.sub(r"\s+", "", text.lower())
    volume_word = re.search(r"音量|声音|volume", compact) or re.fullmatch(
        r"(?:请|帮我)?(?:调|设置)(?:到|为|成)?(?:百分之)?(?:[0-9]{1,3}|[零一二两三四五六七八九十百]{1,4})(?:%|％)",
        compact,
    )
    if not volume_word:
        return None
    if not re.search(r"调|设|提高|降低|增大|减小|大点|小点|up|down|set", compact):
        return None
    target = "auto"
    for pattern, candidate in (
        (r"所有|全部|all", "all"),
        (r"airplay|隔空", "airplay"),
        (r"蓝牙|bluetooth", "bluetooth"),
        (r"耳机|headphone", "headphone"),
        (r"你的声音|播报|通知|tts", "tts"),
        (r"主音量|mpd|本地音乐", "master"),
    ):
        if re.search(pattern, compact):
            target = candidate
            break
    number = re.search(
        r"(?:百分之)?([0-9]{1,3}|[零一二两三四五六七八九十百]{1,4})(?:%|％)?", compact
    )
    if number:
        level = chinese_number(number.group(1))
        if not 0 <= level <= 100:
            raise BridgeError("invalid_volume")
        return {"target": target, "level": level}
    if re.search(r"小|低|down|减", compact):
        return {"target": target, "delta": -10}
    if re.search(r"大|高|up|增", compact):
        return {"target": target, "delta": 10}
    return None


@dataclass
class VolumeDisplayRequest:
    level: float
    probe: tuple[str, float] | None = None
    guarded: bool = False


class VolumeManager:
    def __init__(self, hass, entry, client, on_change=None) -> None:
        self.entry = entry
        self.client = client
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.unique_id}.volume")
        self.desired: dict[str, float] = {}
        self.state: dict = {}
        self.caps: dict = {}
        self.on_change = on_change
        self.generation = 0
        self.write_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()
        self._displayed: dict[str, float | None] = {}
        # Separate in-flight intent from acknowledged intent awaiting a Probe:
        # a newer failed/cancelled request must reveal the earlier success.
        self._requests: dict[str, VolumeDisplayRequest] = {}
        self._confirmed: dict[str, VolumeDisplayRequest] = {}

    @staticmethod
    def _valid_level(value) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= value <= 100
            and math.isfinite(value)
        )

    @staticmethod
    def _temporary(state: dict) -> bool:
        return bool(
            state.get("voice", {}).get("active")
            or state.get("audio_active")
            or state.get("volume_recovery_pending")
        )

    @staticmethod
    def _probe_key(state: dict) -> tuple[str, float] | None:
        instance, updated = state.get("instance_id"), state.get("updated")
        if (
            isinstance(instance, str)
            and instance
            and isinstance(updated, (int, float))
            and not isinstance(updated, bool)
            and math.isfinite(updated)
        ):
            return instance, updated
        return None

    def display_level(self, target: str, state: dict) -> float | None:
        if request := self._requests.get(target) or self._confirmed.get(target):
            return request.level
        if self._temporary(state):
            for value in (
                self.desired.get(target),
                state.get("voice", {}).get("volumes", {}).get(target),
                self._displayed.get(target),
            ):
                if self._valid_level(value):
                    return value
            return None
        value = self._displayed.get(target, state.get("volumes", {}).get(target))
        if self._valid_level(value):
            return value
        value = self.desired.get(target)
        return value if self._valid_level(value) else None

    def observe(self, state: dict, generation: int, *, busy: bool = False) -> dict[str, float]:
        """Accept safe observations; a cached/in-flight Probe cannot undo an intent."""
        if generation != self.generation:
            return {}
        probe = self._probe_key(state)
        baselines = set()
        for target, request in self._confirmed.items():
            if probe is not None and (request.probe is None or request.probe[0] != probe[0]):
                # Establish the cache boundary from the first response read
                # after acknowledgement, including null/temporary samples.
                request.probe, request.guarded = probe, False
                baselines.add(target)
        if busy or state.get("state_stale") or self._temporary(state):
            return {}
        observed = {}
        for target, level in state.get("volumes", {}).items():
            if target in self._requests or not self._valid_level(level):
                continue
            if request := self._confirmed.get(target):
                if (
                    target in baselines
                    or probe is None
                    or request.probe is None
                    or probe[1] <= request.probe[1]
                ):
                    continue
                if level != request.level and not request.guarded:
                    # Probe timestamps describe completion, not mixer sampling.
                    # One Probe may have sampled before the write and completed
                    # afterwards. Quarantine that conflicting cache generation.
                    request.probe, request.guarded = probe, True
                    continue
                del self._confirmed[target]
            self._displayed[target] = observed[target] = level
        return observed

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    async def load(self) -> None:
        saved = await self.store.async_load()
        if isinstance(saved, dict):
            self.desired = {
                k: v for k, v in saved.items() if isinstance(v, (int, float)) and 0 <= v <= 100
            }

    def targets(self, target: str, source: str | None = None) -> list[str]:
        return resolve_targets(target, self.state, self.caps, source)

    def plan(
        self, text: str, snapshot: dict | None = None, local_intent: str | None = None
    ) -> dict:
        if local_intent not in (None, "VolumeUp", "VolumeDown"):
            raise BridgeError("invalid_volume_intent")
        intent = (
            {"target": "auto", "delta": 10 if local_intent == "VolumeUp" else -15}
            if local_intent is not None
            else volume_intent(text)
        )
        if intent is None:
            return {"handled": False}
        source = None
        if snapshot is not None:
            active = snapshot.get("active_sources", [])
            priority = self.entry.options.get("source_priority", list(SOURCE_PRIORITY))
            source = next((s for s in priority if s in active), snapshot.get("source") or "")
        targets = self.targets(intent["target"], source)
        plan = []
        for target in targets:
            level = intent.get("level")
            if level is None:
                current = self.desired.get(target, self.state.get("volumes", {}).get(target))
                if current is None:
                    raise BridgeError("volume_unknown")
                level = max(0, min(100, current + intent["delta"]))
            plan.append({"target": target, "level": level})
        return {"handled": True, "plan": plan, "reply_text": "音量已调整。"}

    async def commit(
        self,
        plan: list[dict],
        *,
        generation: int | None = None,
        acknowledged: bool = False,
    ) -> None:
        async with self._store_lock:
            if generation is not None and generation != self.generation:
                return
            desired = {**self.desired, **{item["target"]: item["level"] for item in plan}}
            try:
                await self.store.async_save(desired)
            except OSError:
                raise BridgeError("state_write_failed") from None
            self.desired = desired
            if acknowledged and plan:
                # LX06 also executes volume plans locally. Its success callback
                # supersedes older confirmations, without hiding pending HA input.
                for item in plan:
                    target, level = item["target"], item["level"]
                    self._displayed[target] = level
                    self._confirmed[target] = VolumeDisplayRequest(level)
                self.generation += 1
                self._changed()

    async def set(self, target: str, level: float, *, source: str | None = None) -> dict:
        if not self._valid_level(level):
            raise BridgeError("invalid_volume")
        plan = [{"target": t, "level": level} for t in self.targets(target, source)]
        requests = {}
        for item in plan:
            channel = item["target"]
            self._displayed.setdefault(channel, self.display_level(channel, self.state))
            requests[channel] = self._requests[channel] = VolumeDisplayRequest(level)
        self.generation += 1
        results = []
        try:
            self._changed()
            async with self.write_lock:
                for item in plan:
                    results.append(
                        await self.client.volume(item["target"], item["level"], uuid.uuid4().hex)
                    )
                    # Only acknowledged, durably saved channels become confirmed.
                    await self.commit([item])
                    channel = item["target"]
                    self._displayed[channel] = level
                    self._confirmed[channel] = requests[channel]
                    if self._requests.get(channel) is requests[channel]:
                        del self._requests[channel]
                    self.generation += 1
                    self._changed()
        finally:
            rolled_back = False
            for channel, request in requests.items():
                if self._requests.get(channel) is request:
                    del self._requests[channel]
                    rolled_back = True
            if rolled_back:
                self.generation += 1
                self._changed()
        return {"targets": [p["target"] for p in plan], "level": level, "results": results}
