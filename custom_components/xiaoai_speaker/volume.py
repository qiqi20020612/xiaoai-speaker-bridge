"""HA owns volume routing, language interpretation and persistent desired values."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from homeassistant.helpers.storage import Store

from .client import BridgeError
from .const import DOMAIN, IDLE_POLICIES, SOURCE_PRIORITY


@dataclass(frozen=True)
class SourceDecision:
    source: str | None
    uncertain: bool = False


def choose_source(state: dict, priority: tuple | list = SOURCE_PRIORITY) -> SourceDecision:
    voice = state.get("voice", {})
    if voice.get("active"):
        active = voice.get("active_sources", [])
        source = next((s for s in priority if s in active), voice.get("source"))
        return SourceDecision(source, voice.get("uncertain", source is None))
    active = []
    for source in priority:
        value = state.get(source, {})
        if value.get("active") or value.get("state") in ("play", "playing", "Playing"):
            active.append(source)
    unknown = state.get("state_stale", False) or any(
        state.get(s, {}).get("state") == "unknown" for s in priority
    )
    return SourceDecision(active[0] if active else None, len(active) > 1 or unknown)


def resolve_targets(
    target: str, state: dict, caps: dict, idle_policy: str = "master", source: str | None = None
) -> list[str]:
    if idle_policy not in IDLE_POLICIES:
        raise BridgeError("invalid_idle_policy")
    source = source if source is not None else choose_source(state).source
    if target == "auto":
        target = {"mpd": "master", "bluetooth": "bluetooth", "airplay": "airplay"}.get(
            source, idle_policy
        )
    targets = ["master", "tts"] if target == "master_tts" else [target]
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


class VolumeManager:
    def __init__(self, hass, entry, client) -> None:
        self.entry = entry
        self.client = client
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.unique_id}.volume")
        self.desired: dict[str, float] = {}
        self.state: dict = {}
        self.caps: dict = {}

    async def load(self) -> None:
        saved = await self.store.async_load()
        if isinstance(saved, dict):
            self.desired = {
                k: v for k, v in saved.items() if isinstance(v, (int, float)) and 0 <= v <= 100
            }

    @property
    def idle_policy(self) -> str:
        return self.entry.options.get("idle_policy", "master")

    def targets(self, target: str, source: str | None = None) -> list[str]:
        return resolve_targets(target, self.state, self.caps, self.idle_policy, source)

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

    async def commit(self, plan: list[dict]) -> None:
        for item in plan:
            self.desired[item["target"]] = item["level"]
        await self.store.async_save(self.desired)

    async def set(self, target: str, level: float, *, source: str | None = None) -> dict:
        if not 0 <= level <= 100:
            raise BridgeError("invalid_volume")
        plan = [{"target": t, "level": level} for t in self.targets(target, source)]
        results = []
        for item in plan:
            results.append(
                await self.client.volume(item["target"], item["level"], uuid.uuid4().hex)
            )
            # Persist only successfully acknowledged channels; expose partial failures.
            await self.commit([item])
        return {"targets": [p["target"] for p in plan], "level": level, "results": results}
