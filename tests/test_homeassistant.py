import asyncio
import json
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaoai_speaker.client import AuthenticationError
from custom_components.xiaoai_speaker.const import DOMAIN

INFO = {
    "device_id": "synthetic-speaker",
    "model": "LX06",
    "protocol": 1,
    "version": "0.1.7",
    "capabilities": {
        "volume": {"master": True, "tts": True, "bluetooth": True, "airplay": False},
        "playback": {"airplay": {"actions": []}, "bluetooth": {"actions": []}},
        "announce": True,
        "conversation": True,
        "listener": True,
    },
}
STATE = {
    "volumes": {"master": 60, "tts": 55, "bluetooth": 40},
    "airplay": {"state": "playing", "active": True, "volume": 35, "raw_volume": -19.5},
    "bluetooth": {"state": "stopped", "active": False},
    "voice": {"active": False},
    "listener": {"running": True},
    "system": {"memory_available": 50000, "bridge_rss": 3000, "cpu_load": 0.15},
}
DATA = {
    CONF_HOST: "speaker.example",
    CONF_PORT: 8766,
    CONF_TOKEN: "synthetic-token-that-is-not-a-real-credential",
}


async def long_events(*args):
    await asyncio.sleep(3600)


@pytest.fixture
def mock_clients():
    with (
        patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.info",
            AsyncMock(return_value=INFO),
        ),
        patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.state",
            AsyncMock(return_value=STATE),
        ),
        patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.events", side_effect=long_events
        ),
        patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.request",
            AsyncMock(return_value={"accepted": True}),
        ),
        patch(
            "custom_components.xiaoai_speaker.mpd.MPDClient.discover",
            AsyncMock(return_value={"play", "pause", "stop"}),
        ),
        patch(
            "custom_components.xiaoai_speaker.mpd.MPDClient.snapshot",
            AsyncMock(return_value={"state": "stop", "online": True}),
        ),
    ):
        yield


async def test_config_flow_identity_and_auth_errors(hass, mock_clients):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == FlowResultType.FORM
    with patch(
        "custom_components.xiaoai_speaker.client.BridgeClient.info",
        AsyncMock(side_effect=AuthenticationError("auth_failed")),
    ):
        failed = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert failed["errors"]["base"] == "invalid_auth"
    with patch("homeassistant.config_entries.ConfigEntries.async_setup", return_value=True):
        done = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert done["result"].unique_id == "synthetic-speaker"


async def test_setup_native_entities_without_linn_and_clean_unload(hass, mock_clients):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Synthetic speaker", data=DATA, unique_id="synthetic-speaker"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    media = hass.states.get("media_player.synthetic_speaker")
    assert media is not None
    assert media.state == "playing"
    assert media.attributes["source"] == "airplay"
    assert "linn" not in hass.config.components
    assert hass.states.get("number.synthetic_speaker_master_volume") is not None
    assert hass.services.has_service(DOMAIN, "announce")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_services_require_a_target_and_keep_announcement_separate(hass, mock_clients):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Synthetic speaker", data=DATA, unique_id="synthetic-speaker"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "announce", {"text": "打开灯"}, blocking=True)
    coordinator = entry.runtime_data.coordinator
    with patch.object(coordinator.jobs, "submit", AsyncMock()) as submit:
        submit.return_value.request_id = "test-job"
        submit.return_value.status = "accepted"
        response = await hass.services.async_call(
            DOMAIN,
            "announce",
            {"entity_id": "media_player.synthetic_speaker", "text": "打开灯"},
            blocking=True,
            return_response=True,
        )
    assert response["request_id"] == "test-job"
    assert submit.call_args.args[0] == "announce"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_have_only_retained_configuration(hass, mock_clients):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Synthetic speaker", data=DATA, unique_id="synthetic-speaker"
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    json.dumps(
        probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "poll_interval": 15,
            "source_priority_1": "airplay",
            "source_priority_2": "bluetooth",
            "source_priority_3": "mpd",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert "idle_policy" not in entry.options
    assert entry.options["source_priority"] == ["airplay", "bluetooth", "mpd"]


async def test_options_serialization_order_validation_and_reopen(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=DATA,
        unique_id="synthetic-speaker",
        options={
            "source_priority": ["mpd", "airplay", "bluetooth"],
            "agent_id": "device_default",
            "announce_ducking": False,
            "announce_duck_volume": 75,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    json.dumps(fields)
    assert {field["name"] for field in fields} == {
        "poll_interval",
        "source_priority_1",
        "source_priority_2",
        "source_priority_3",
    }
    assert [
        field["default"] for field in fields if field["name"].startswith("source_priority_")
    ] == ["mpd", "airplay", "bluetooth"]
    values = {
        "poll_interval": 10,
        "source_priority_1": "mpd",
        "source_priority_2": "mpd",
        "source_priority_3": "airplay",
    }
    failed = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert failed["errors"]["base"] == "invalid_source_priority"
    assert entry.options["source_priority"] == ["mpd", "airplay", "bluetooth"]
    json.dumps(
        probatio.to_field_list(failed["data_schema"], custom_serializer=cv.custom_serializer)
    )
    values["source_priority_2"] = "bluetooth"
    saved = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert saved["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["source_priority"] == ["mpd", "bluetooth", "airplay"]
    assert "agent_id" not in entry.options
    assert "announce_ducking" not in entry.options and "announce_duck_volume" not in entry.options
    assert "source_priority_1" not in entry.options
    reopened = await hass.config_entries.options.async_init(entry.entry_id)
    fields = probatio.to_field_list(reopened["data_schema"], custom_serializer=cv.custom_serializer)
    assert [
        field["default"] for field in fields if field["name"].startswith("source_priority_")
    ] == ["mpd", "bluetooth", "airplay"]


async def test_options_reject_unknown_or_missing_sources(hass):
    from custom_components.xiaoai_speaker.config_flow import XiaoAIOptionsFlow

    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options={})
    entry.add_to_hass(hass)
    flow = XiaoAIOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    for values in [
        {
            "source_priority_1": "unknown",
            "source_priority_2": "mpd",
            "source_priority_3": "airplay",
        },
        {},
    ]:
        result = await flow.async_step_init(values)
        assert result["errors"]["base"] == "invalid_source_priority"
