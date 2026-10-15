"""v0.1.7 registry, service and source contracts against real HA 2026.10."""

import copy
import subprocess
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaoai_speaker.button import ManagementButton
from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.const import DOMAIN
from tests.test_capabilities import setup
from tests.test_homeassistant import DATA, STATE
from tests.test_homeassistant import mock_clients as mock_clients
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import text_device as text_device

RETAINED = {
    "media_player": {"player"},
    "text": {"announce_text", "send_text_text"},
    "number": {"volume_master", "volume_tts", "volume_bluetooth"},
    "sensor": {"airplay_volume", "memory_available", "bridge_rss", "cpu_load"},
    "button": {"refresh", "restart_listener", "reset_context"},
    "switch": {"listener"},
}
RETIRED = {
    "light": {"led"},
    "sensor": {
        "active_source",
        "airplay_raw_volume",
        "conversation_remaining",
        "queue_size",
        "job_status",
        "uptime",
        "memory_total",
        "bridge_peak_rss",
    },
    "binary_sensor": {"online", "voice_active", "airplay_active", "source_uncertain"},
    "select": {"agent", "idle_policy"},
    "switch": {"speak_response", "use_context"},
    "button": {"announce_send", "send_text_send"},
    "number": {"volume_airplay", "volume_headphone"},
}


async def test_upgrade_has_exactly_14_identities_and_preserves_all_retained_customizations(
    hass, mock_clients
):
    options = {
        "agent_id": "conversation.old_override",
        "idle_policy": "master_tts",
        "source_priority": ["mpd", "airplay", "bluetooth"],
        "announce_ducking": True,
        "announce_duck_volume": 75,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic speaker",
        data=DATA,
        unique_id="synthetic-speaker",
        options=options,
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    previous = []
    retired = []
    for catalog, entries in ((RETAINED, previous), (RETIRED, retired)):
        for domain, keys in catalog.items():
            for key in sorted(keys):
                entity = registry.async_get_or_create(
                    domain,
                    DOMAIN,
                    f"synthetic-speaker_{key}",
                    config_entry=entry,
                    suggested_object_id=f"renamed_{key}",
                    disabled_by=er.RegistryEntryDisabler.USER if len(entries) % 2 else None,
                )
                entries.append(registry.async_update_entity(entity.entity_id, name=f"My {key}"))
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.unique_id)}, name="My speaker"
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    expected = {
        (domain, f"synthetic-speaker_{key}") for domain, keys in RETAINED.items() for key in keys
    }
    actual = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(actual) == 14 and {(e.domain, e.unique_id) for e in actual} == expected
    for old in previous:
        new = registry.async_get(old.entity_id)
        assert (new.unique_id, new.name, new.disabled_by) == (
            old.unique_id,
            old.name,
            old.disabled_by,
        )
    assert all(registry.async_get(e.entity_id) is None for e in retired)
    assert dr.async_get(hass).async_get(device.id).identifiers == device.identifiers
    assert entry.data == DATA and entry.unique_id == "synthetic-speaker"
    assert entry.options == {
        k: v
        for k, v in options.items()
        if k not in ("agent_id", "idle_policy", "announce_ducking", "announce_duck_volume")
    }
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 14
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_retired_cleanup_does_not_match_other_domains_entries_or_platforms(
    hass, mock_clients
):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Synthetic speaker", data=DATA, unique_id="synthetic-speaker"
    )
    other = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="other-speaker")
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    survivors = []
    for domain, keys in RETIRED.items():
        for key in keys:
            uid = f"synthetic-speaker_{key}"
            for platform, owner, identifier in (
                ("other_integration", entry, uid),
                (DOMAIN, other, uid),
                (DOMAIN, entry, uid + "_extra"),
            ):
                survivors.append(
                    registry.async_get_or_create(domain, platform, identifier, config_entry=owner)
                )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert all(registry.async_get(e.entity_id) is not None for e in survivors)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_airplay_single_percent_sensor_has_raw_db_attribute(hass, mock_clients):
    entry, c = await setup(hass)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "synthetic-speaker_airplay_volume"
    )
    state = hass.states.get(entity_id)
    assert state.state == "35" and state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["raw_volume"] == -19.5 and state.attributes["raw_volume_unit"] == "dB"
    with patch.object(
        c.client,
        "state",
        AsyncMock(return_value={**STATE, "airplay": {"active": None, "state": "unknown"}}),
    ):
        await c.async_refresh()
    state = hass.states.get(entity_id)
    assert state.state == "unknown" and state.attributes["raw_volume"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "source,target",
    [(None, "master"), ("airplay", "airplay"), ("bluetooth", "bluetooth"), ("mpd", "master")],
)
async def test_media_and_volume_reuse_one_source_decision(hass, mock_clients, source, target):
    entry, c = await setup(hass, {"idle_policy": "tts"})
    c.info = copy.deepcopy(c.info)
    c.info["capabilities"]["volume"] = {
        "master": True,
        "tts": True,
        "bluetooth": True,
        "airplay": True,
    }
    c.volume.caps = c.info["capabilities"]
    snapshot = {
        **copy.deepcopy(STATE),
        "volumes": {"master": 60, "tts": 55, "bluetooth": 40, "airplay": 35},
        "capabilities": c.info["capabilities"],
    }
    snapshot["airplay"] = {
        "active": source == "airplay",
        "state": "playing" if source == "airplay" else "stopped",
    }
    snapshot["bluetooth"] = {
        "active": source == "bluetooth",
        "state": "playing" if source == "bluetooth" else "stopped",
    }
    with (
        patch.object(c.client, "state", AsyncMock(return_value=snapshot)),
        patch.object(
            c.mpd,
            "snapshot",
            AsyncMock(return_value={"state": "play" if source == "mpd" else "stop"}),
        ),
        patch(
            "custom_components.xiaoai_speaker.volume.choose_source",
            side_effect=AssertionError("Repeated source computation"),
        ),
    ):
        await c.async_refresh()
        player = hass.states.get("media_player.synthetic_speaker")
        assert player.state == ("playing" if source else "idle")
        assert (
            player.attributes.get("source") == source
            and player.attributes["active_source"] == source
        )
        assert "activity" not in player.attributes
        assert c.volume.targets("auto") == [target]
        assert player.attributes["volume_level"] == snapshot["volumes"][target] / 100
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_refresh_never_syncs_retired_agent_or_idle_policy(hass, mock_clients):
    entry, c = await setup(hass, {"agent_id": "conversation.old", "idle_policy": "tts"})
    with patch.object(c.client, "request", AsyncMock(return_value={"accepted": True})) as request:
        for instance in ("first-daemon", "restarted-daemon"):
            with patch.object(
                c.client,
                "state",
                AsyncMock(return_value={**copy.deepcopy(STATE), "instance_id": instance}),
            ):
                await c.async_refresh()
        assert request.await_count == 2
        for call in request.await_args_list:
            assert call.args[1] == "/api/policy"
            assert set(call.args[2]) == {"source", "uncertain", "priority"}
    assert "job" not in c.data
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("flags", [{}, {"speak_response": False, "use_context": False}])
async def test_send_text_services_keep_flags_and_reject_removed_agent(hass, mock_clients, flags):
    entry, c = await setup(hass, {"agent_id": "conversation.old_override"})
    data = {"entity_id": "media_player.synthetic_speaker", "text": "hello", **flags}
    with patch.object(c.jobs, "submit", AsyncMock()) as submit:
        submit.return_value.request_id = "test-job"
        submit.return_value.status = "accepted"
        await hass.services.async_call(DOMAIN, "send_text", data, blocking=True)
        payload = submit.call_args.args[1]
        assert payload["speak_response"] is flags.get("speak_response", True)
        assert payload["use_context"] is flags.get("use_context", True)
        assert "agent_id" not in payload and not hasattr(c.quick, "settings")
        with pytest.raises(probatio.Invalid):
            await hass.services.async_call(
                DOMAIN, "send_text", {**data, "agent_id": "conversation.other"}, blocking=True
            )
        assert submit.call_count == 1
    assert not hass.services.has_service(DOMAIN, "set_idle_volume_policy")
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("error", [None, "restart_unavailable", "busy"])
async def test_restart_button_only_schedules_bridge_without_wait_or_refresh(
    hass, mock_clients, error
):
    entry, c = await setup(hass)
    button = ManagementButton(c, "restart_bridge")
    assert button.unique_id == "synthetic-speaker_restart_listener"
    with (
        patch.object(
            c.client, "request", AsyncMock(side_effect=BridgeError(error) if error else None)
        ) as request,
        patch.object(c, "async_request_refresh", AsyncMock()) as refresh,
    ):
        if error:
            with pytest.raises(HomeAssistantError, match=error):
                await button.async_press()
        else:
            await button.async_press()
        request.assert_awaited_once_with("POST", "/api/bridge/restart", {}, timeout=3)
        refresh.assert_not_called()
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_reset_context_targets_native_cache_even_with_old_override(text_device, speaker_shell):
    call, path, env = text_device
    call("voice", "my name is Alice", speak_response=False)
    listener = (path / "existing-listener").read_bytes()
    override = path / "xiaoai_bridge.context.conversation.other.json"
    override.write_text("keep old bytes for rollback")
    (path / "xiaoai_bridge.agent.json").write_text('{"agent_id":"conversation.other"}')
    device = path / "bridge/lib/device.sh"
    # Rebase only the filesystem safety boundary to the isolated fixture.
    device.write_text(device.read_text().replace("/tmp/*)", f"{path}/*)"))
    result = subprocess.run(
        [*speaker_shell, str(device), "reset_context"],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0 and '"completed":true' in result.stdout
    assert not (path / "eoai_conversation.json").exists()
    assert override.read_text() == "keep old bytes for rollback"
    assert (path / "existing-listener").read_bytes() == listener
    assert call("send_text", "what is my name", speak_response=False)[1]["reply_text"] == "unknown"
