"""Real HA entity/registry/service behavior across device capability transitions."""

import copy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.const import DOMAIN
from tests.test_homeassistant import DATA, INFO, STATE
from tests.test_homeassistant import mock_clients as mock_clients


async def setup(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic speaker",
        data=DATA,
        unique_id="synthetic-speaker",
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data.coordinator


async def test_late_capabilities_loss_recovery_and_offline_keep_identity(hass, mock_clients):
    info = copy.deepcopy(INFO)
    info["capabilities"]["conversation"] = False
    info["capabilities"]["volume"]["airplay"] = False
    state = {**STATE, "capabilities": info["capabilities"]}
    with patch(
        "custom_components.xiaoai_speaker.client.BridgeClient.info", AsyncMock(return_value=info)
    ):
        with patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.state",
            AsyncMock(return_value=state),
        ):
            entry, c = await setup(hass)
    registry = er.async_get(hass)
    uid = "synthetic-speaker_reset_context"
    assert registry.async_get_entity_id("button", DOMAIN, uid) is None
    caps = copy.deepcopy(INFO["capabilities"])
    caps["volume"]["airplay"] = True
    with patch.object(c.client, "state", AsyncMock(return_value={**STATE, "capabilities": caps})):
        await c.async_refresh()
        await hass.async_block_till_done()
    button = registry.async_get_entity_id("button", DOMAIN, uid)
    assert button and hass.states.get(button).state != "unavailable"
    assert registry.async_get_entity_id("number", DOMAIN, "synthetic-speaker_volume_airplay")
    size = len(registry.entities)
    caps["conversation"] = False
    with patch.object(c.client, "state", AsyncMock(return_value={**STATE, "capabilities": caps})):
        await c.async_refresh()
    assert hass.states.get(button).state == "unavailable"
    with patch.object(c.jobs, "submit", AsyncMock()) as submit:
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                "send_text",
                {"entity_id": "media_player.synthetic_speaker", "text": "test"},
                blocking=True,
            )
        submit.assert_not_called()
    caps["conversation"] = True
    with patch.object(c.client, "state", AsyncMock(return_value={**STATE, "capabilities": caps})):
        await c.async_refresh()
        await c.async_refresh()
        await hass.async_block_till_done()
    assert registry.async_get_entity_id("button", DOMAIN, uid) == button
    assert len(registry.entities) == size
    with patch.object(c.client, "state", AsyncMock(side_effect=BridgeError("connection_failed"))):
        await c.async_refresh()
    assert hass.states.get(button).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_legacy_sensor_ids_and_manual_enabled_choice_are_preserved(hass, mock_clients):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Synthetic speaker", data=DATA, unique_id="synthetic-speaker"
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "synthetic-speaker_bridge_peak_rss",
        suggested_object_id="my_existing_peak",
        config_entry=entry,
        disabled_by=None,
    )
    enabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "synthetic-speaker_memory_available",
        suggested_object_id="my_existing_memory",
        config_entry=entry,
        disabled_by=None,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(legacy.entity_id) is not None
    assert hass.states.get(enabled.entity_id) is not None
    assert registry.async_get(legacy.entity_id).disabled_by is None
    assert registry.async_get_entity_id("sensor", DOMAIN, "synthetic-speaker_memory_total") is None
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, "synthetic-speaker_airplay_raw_volume")
        is None
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_quick_inputs_only_send_on_click_use_services_and_block_duplicate(hass, mock_clients):
    entry, c = await setup(hass)
    registry = er.async_get(hass)
    text_id = registry.async_get_entity_id("text", DOMAIN, "synthetic-speaker_send_text_text")
    button = registry.async_get_entity_id("button", DOMAIN, "synthetic-speaker_send_text_send")
    speak = registry.async_get_entity_id("switch", DOMAIN, "synthetic-speaker_speak_response")
    context = registry.async_get_entity_id("switch", DOMAIN, "synthetic-speaker_use_context")
    assert hass.states.get(speak).state == hass.states.get(context).state == "on"
    with patch.object(c.jobs, "submit", wraps=c.jobs.submit) as submit:
        await hass.services.async_call(
            "text",
            "set_value",
            {"entity_id": text_id, "value": "synthetic non-sensitive instruction"},
            blocking=True,
        )
        submit.assert_not_called()
        await hass.services.async_call("switch", "turn_off", {"entity_id": speak}, blocking=True)
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
        assert submit.call_count == 1
        assert submit.call_args.args[0] == "send_text"
        assert submit.call_args.args[1]["speak_response"] is False
        assert submit.call_args.args[1]["use_context"] is True
        assert hass.states.get(text_id).state == ""
        await hass.services.async_call(
            "text",
            "set_value",
            {"entity_id": text_id, "value": "another instruction"},
            blocking=True,
        )
        with pytest.raises(HomeAssistantError, match="quick_action_busy"):
            await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
        assert submit.call_count == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_text_privacy_is_explicit_and_recorder_filter_excludes_states(
    hass, mock_clients
):
    from homeassistant.helpers.entityfilter import generate_filter

    from custom_components.xiaoai_speaker.diagnostics import async_get_config_entry_diagnostics

    entry, c = await setup(hass)
    registry = er.async_get(hass)
    text_id = registry.async_get_entity_id("text", DOMAIN, "synthetic-speaker_announce_text")
    marker = "synthetic-private-marker"
    await hass.services.async_call(
        "text", "set_value", {"entity_id": text_id, "value": marker}, blocking=True
    )
    assert (
        hass.states.get(text_id).state == marker
    )  # Never claim native TextEntity is secret storage.
    recorder_filter = generate_filter([], [], [], [text_id])
    assert not recorder_filter(text_id)
    assert recorder_filter("media_player.synthetic_speaker")
    assert marker not in str(await async_get_config_entry_diagnostics(hass, entry))
    await hass.services.async_call(
        "text", "set_value", {"entity_id": text_id, "value": ""}, blocking=True
    )
    button = registry.async_get_entity_id("button", DOMAIN, "synthetic-speaker_announce_send")
    with pytest.raises(HomeAssistantError, match="invalid_text"):
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_ducking_options_are_applied_only_to_speaking_jobs(hass, mock_clients):
    entry, c = await setup(hass, {"announce_ducking": True, "announce_duck_volume": 10})
    c.info["capabilities"] = {**c.info["capabilities"], "announcement_ducking": True}
    with patch.object(c.jobs, "submit", wraps=c.jobs.submit) as submit:
        await hass.services.async_call(
            DOMAIN,
            "announce",
            {"entity_id": "media_player.synthetic_speaker", "text": "test"},
            blocking=True,
        )
        assert submit.call_args.args[1]["duck_volume"] == 10
        await hass.services.async_call(
            DOMAIN,
            "send_text",
            {
                "entity_id": "media_player.synthetic_speaker",
                "text": "silent",
                "speak_response": False,
            },
            blocking=True,
        )
        assert "duck_volume" not in submit.call_args.args[1]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pause_refresh_clears_active_airplay_source(hass, mock_clients):
    entry, c = await setup(hass)
    assert c.data["active_source"] == "airplay"
    with patch.object(
        c.client,
        "state",
        AsyncMock(return_value={**STATE, "airplay": {"active": False, "state": "Stopped"}}),
    ):
        await c.async_refresh()
    assert c.data["active_source"] is None
    assert hass.states.get("media_player.synthetic_speaker").state == "idle"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_v013_protocol_without_duck_capability_keeps_existing_announce_working(
    hass, mock_clients
):
    entry, c = await setup(hass, {"announce_ducking": True, "announce_duck_volume": 10})
    c.info["capabilities"] = {
        k: v for k, v in c.info["capabilities"].items() if k != "announcement_ducking"
    }
    with patch.object(c.jobs, "submit", wraps=c.jobs.submit) as submit:
        await hass.services.async_call(
            DOMAIN,
            "announce",
            {"entity_id": "media_player.synthetic_speaker", "text": "test"},
            blocking=True,
        )
        assert "duck_volume" not in submit.call_args.args[1]
    assert await hass.config_entries.async_unload(entry.entry_id)
