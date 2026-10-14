import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.const import DOMAIN
from custom_components.xiaoai_speaker.volume import (
    VolumeManager,
    choose_source,
    resolve_targets,
    volume_intent,
)

CAPS = {
    "volume": {"master": True, "tts": True, "bluetooth": True, "headphone": False, "airplay": False}
}


def test_voice_source_is_frozen_before_tts():
    state = {
        "voice": {"active": True, "source": "airplay", "uncertain": False},
        "mpd": {"state": "play"},
        "tts": {"active": True},
    }
    assert choose_source(state).source == "airplay"
    with pytest.raises(BridgeError, match="unsupported_volume_target"):
        resolve_targets("auto", {**state, "active_source": choose_source(state).source}, CAPS)
    assert resolve_targets("tts", state, CAPS) == ["tts"]


def test_idle_target_is_master_and_ignores_tts_playback():
    assert resolve_targets("auto", {"tts": {"active": True}}, CAPS) == ["master"]


def test_all_preflights_airplay_and_prevents_double_attenuation():
    state = {"airplay": {"active": True}, "active_source": "airplay"}
    with pytest.raises(BridgeError):
        resolve_targets("all", state, CAPS)
    caps = {"volume": {**CAPS["volume"], "airplay": True}}
    result = resolve_targets("all", state, caps)
    assert "airplay" in result and "master" not in result


def test_conflicting_sources_use_configured_priority_and_flag_uncertainty():
    result = choose_source(
        {"mpd": {"state": "play"}, "bluetooth": {"active": True}}, ["mpd", "bluetooth", "airplay"]
    )
    assert result.source == "mpd" and result.uncertain


@pytest.mark.parametrize(
    "state,priority,source,uncertain",
    [
        (
            {
                "airplay": {"active": True, "state": "Playing"},
                "bluetooth": {"active": False, "state": "unknown"},
                "mpd": {"state": "unknown"},
            },
            ["airplay", "bluetooth", "mpd"],
            "airplay",
            False,
        ),
        (
            {"airplay": {"state": "unknown"}, "mpd": {"state": "play"}},
            ["airplay", "bluetooth", "mpd"],
            "mpd",
            True,
        ),
        (
            {
                "airplay": {"active": False, "state": "Playing"},
                "bluetooth": {"active": False, "state": "unknown"},
                "mpd": {"state": "stop"},
            },
            ["airplay", "bluetooth", "mpd"],
            None,
            False,
        ),
        (
            {"state_stale": True, "airplay": {"active": True}},
            ["airplay", "bluetooth", "mpd"],
            "airplay",
            True,
        ),
        (
            {"airplay": {"active": True}, "mpd": {"state": "play"}},
            ["mpd", "airplay", "bluetooth"],
            "mpd",
            True,
        ),
        ({}, ["airplay", "bluetooth", "mpd"], None, True),
    ],
)
def test_source_confidence_refers_to_selected_source(state, priority, source, uncertain):
    result = choose_source(state, priority)
    assert (result.source, result.uncertain) == (source, uncertain)


def test_voice_confidence_uses_frozen_observations_and_configured_priority():
    state = {
        "voice": {
            "active": True,
            "source": "airplay",
            "uncertain": True,
            "observed_sources": {"airplay": True, "bluetooth": None, "mpd": False},
        },
        "mpd": {"state": "play"},
    }
    assert choose_source(state).source == "airplay"
    assert not choose_source(state).uncertain
    assert choose_source(state, ["bluetooth", "airplay", "mpd"]).uncertain


@pytest.mark.parametrize(
    "text,expected",
    [
        ("把音量调到百分之三十", {"target": "auto", "level": 30}),
        ("调到30%", {"target": "auto", "level": 30}),
        ("把你的声音调到30%", {"target": "tts", "level": 30}),
        ("把 AirPlay 音量调到 30%", {"target": "airplay", "level": 30}),
        ("把所有音量调到一百", {"target": "all", "level": 100}),
        ("把蓝牙音量调小", {"target": "bluetooth", "delta": -10}),
        ("打开客厅的灯", None),
    ],
)
def test_volume_language_is_interpreted_in_ha(text, expected):
    assert volume_intent(text) == expected


async def test_native_volume_intents_use_frozen_source_and_fixed_master(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="synthetic", options={"idle_policy": "tts"})
    manager = VolumeManager(hass, entry, None)
    manager.caps = CAPS
    manager.state = {"mpd": {"state": "play"}, "volumes": {"master": 60, "tts": 55}}
    with pytest.raises(BridgeError, match="unsupported_volume_target"):
        manager.plan("大声点", {"source": "airplay"}, "VolumeUp")
    up = manager.plan("大声点", {"source": None}, "VolumeUp")
    down = manager.plan("小声点", {"source": None}, "VolumeDown")
    assert up["plan"] == [{"target": "master", "level": 70}]
    assert down["plan"] == [{"target": "master", "level": 45}]
