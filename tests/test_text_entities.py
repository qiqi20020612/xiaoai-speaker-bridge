"""Native text actions and narrowly scoped legacy send-button migration."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.const import DOMAIN
from tests.test_capabilities import setup
from tests.test_homeassistant import DATA
from tests.test_homeassistant import mock_clients as mock_clients


async def set_text(hass, entity_id, value):
    await hass.services.async_call(
        "text", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_retains_full_successful_input_until_next_submission(hass, mock_clients, kind):
    entry, c = await setup(hass)
    registry = er.async_get(hass)
    text_id = registry.async_get_entity_id("text", DOMAIN, f"synthetic-speaker_{kind}_text")
    other = "send_text" if kind == "announce" else "announce"
    other_id = registry.async_get_entity_id("text", DOMAIN, f"synthetic-speaker_{other}_text")
    with patch.object(c.client, "submit", AsyncMock(return_value={"status": "completed"})):
        await set_text(hass, other_id, "另一实体的已提交文字")
        async with asyncio.timeout(2):
            await c.jobs.jobs[c.quick.requests[other]].done.wait()
        for value in ('  第一行：完整文字\n第二行：引号"、emoji 👋  ', "字" * 255):
            await set_text(hass, text_id, value)
            assert hass.states.get(text_id).state == value
            async with asyncio.timeout(2):
                await c.jobs.jobs[c.quick.requests[kind]].done.wait()
            c.async_set_updated_data(dict(c.data))
            assert hass.states.get(text_id).state == value
            assert hass.states.get(text_id).attributes["status"] == "completed"
            assert hass.states.get(other_id).state == "另一实体的已提交文字"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_submission_failure_preserves_last_successful_value(hass, mock_clients, kind):
    entry, c = await setup(hass)
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )
    previous = "  上次成功的完整文字 👋  "
    with patch.object(c.client, "submit", AsyncMock(return_value={"status": "completed"})):
        await set_text(hass, text_id, previous)
        request_id = c.quick.requests[kind]
        async with asyncio.timeout(2):
            await c.jobs.jobs[request_id].done.wait()
        with patch.object(
            c.jobs, "submit", AsyncMock(side_effect=BridgeError("queue_full"))
        ) as submit:
            with pytest.raises(HomeAssistantError, match="invalid_text"):
                await set_text(hass, text_id, " \n\t")
            submit.assert_not_called()
            with pytest.raises(HomeAssistantError, match="queue_full"):
                await set_text(hass, text_id, "被拒绝的新文字")
            assert submit.call_count == 1
        assert hass.states.get(text_id).state == previous
        assert hass.states.get(text_id).attributes["request_id"] == request_id
        assert not c.quick.pending
        await set_text(hass, text_id, "下次成功的文字")
        assert hass.states.get(text_id).state == "下次成功的文字"
        async with asyncio.timeout(2):
            await c.jobs.jobs[c.quick.requests[kind]].done.wait()
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
@pytest.mark.parametrize("value", ["", " \n\t", "nul\0text", "x" * 256, None, 123])
async def test_text_rejects_invalid_input_without_submission(hass, mock_clients, kind, value):
    entry, c = await setup(hass)
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )
    with patch.object(c.jobs, "submit", wraps=c.jobs.submit) as submit:
        error = ValueError if isinstance(value, str) and len(value) > 255 else HomeAssistantError
        with pytest.raises(error):
            if isinstance(value, str):
                await set_text(hass, text_id, value)
            else:
                await hass.data["text"].get_entity(text_id).async_set_value(value)
        submit.assert_not_called()
    assert hass.states.get(text_id).state == "" and not c.quick.pending
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
@pytest.mark.parametrize("unavailable", ["offline", "capability", "stale"])
async def test_text_rejects_unavailable_device_or_capability(
    hass, mock_clients, caplog, kind, unavailable
):
    entry, c = await setup(hass)
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )
    if unavailable == "offline":
        c.async_set_update_error(BridgeError("connection_failed"))
    else:
        if unavailable == "capability":
            c.info = {
                **c.info,
                "capabilities": {
                    **c.info["capabilities"],
                    "announce" if kind == "announce" else "conversation": False,
                },
            }
        c.async_set_updated_data({**c.data, "state_stale": unavailable == "stale"})
    assert hass.states.get(text_id).state == "unavailable"
    with patch.object(c.jobs, "submit", wraps=c.jobs.submit) as submit:
        # HA's native entity service filters unavailable targets and warns.
        # The entity guard and the original integration service still raise.
        await set_text(hass, text_id, "test")
        assert "missing or not currently available" in caplog.text
        with pytest.raises(HomeAssistantError, match="device_or_capability_unavailable"):
            await hass.data["text"].get_entity(text_id).async_set_value("test")
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN, kind, {"entity_id": text_id, "text": "test"}, blocking=True
            )
        submit.assert_not_called()
    assert not c.quick.pending
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_rejects_concurrent_submission_before_service_returns(hass, mock_clients, kind):
    entry, c = await setup(hass)
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original_submit = c.jobs.submit

    async def submit(kind, payload, context):
        entered.set()
        await release.wait()
        return await original_submit(kind, payload, context)

    with patch.object(c.jobs, "submit", side_effect=submit) as submit_job:
        first = asyncio.create_task(set_text(hass, text_id, "first instruction"))
        try:
            async with asyncio.timeout(2):
                await entered.wait()
            assert hass.states.get(text_id).state == ""
            with pytest.raises(HomeAssistantError, match="quick_action_busy"):
                await set_text(hass, text_id, "second instruction")
        finally:
            release.set()
            await first
        assert submit_job.call_count == 1
        assert submit_job.call_args.args[1]["text"] == "first instruction"
    assert hass.states.get(text_id).state == "first instruction" and not c.quick.pending
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_keeps_service_errors_and_task_failure_feedback(hass, mock_clients, kind):
    entry, c = await setup(hass, {"agent_id": "conversation.existing"})
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )
    with patch.object(c.jobs, "submit", AsyncMock(side_effect=BridgeError("queue_full"))):
        with pytest.raises(HomeAssistantError, match="queue_full"):
            await set_text(hass, text_id, "test")
    assert not c.quick.pending and kind not in c.quick.requests
    assert hass.states.get(text_id).state == ""
    with patch.object(c.client, "submit", AsyncMock(side_effect=BridgeError("synthetic_failure"))):
        await set_text(hass, text_id, "test")
        request_id = c.quick.requests[kind]
        async with asyncio.timeout(2):
            await c.jobs.jobs[request_id].done.wait()
    state = hass.states.get(text_id)
    assert state.attributes["status"] == "failed"
    assert state.attributes["error"] == "synthetic_failure"
    assert state.attributes["request_id"] == request_id and state.state == "test"
    assert "agent_id" not in entry.options
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_upgrade_removes_only_own_send_buttons_and_preserves_text_registry(
    hass, mock_clients
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic speaker",
        data=DATA,
        unique_id="synthetic-speaker",
        options={"agent_id": "conversation.existing", "idle_policy": "tts"},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    texts = [
        registry.async_get_or_create(
            "text",
            DOMAIN,
            f"synthetic-speaker_{kind}_text",
            config_entry=entry,
            suggested_object_id=f"my_{kind}_control",
            disabled_by=er.RegistryEntryDisabler.USER if kind == "send_text" else None,
        )
        for kind in ("announce", "send_text")
    ]
    for entity in texts:
        registry.async_update_entity(
            entity.entity_id, name="My custom control", icon="mdi:message-text"
        )
    retired = [
        registry.async_get_or_create(
            "button",
            DOMAIN,
            f"synthetic-speaker_{kind}_send",
            config_entry=entry,
            suggested_object_id=f"old_{kind}_send",
        ).entity_id
        for kind in ("announce", "send_text")
    ]
    retained = [
        registry.async_get_or_create(
            "button",
            DOMAIN,
            f"synthetic-speaker_{key}",
            config_entry=entry,
            suggested_object_id=f"existing_{key}",
            disabled_by=None,
        ).entity_id
        for key in ("refresh", "reset_context", "restart_listener", "announce_send_extra")
    ]
    foreign_platform = registry.async_get_or_create(
        "button",
        "other_integration",
        "synthetic-speaker_announce_send",
        config_entry=entry,
        suggested_object_id="foreign_platform_button",
    ).entity_id
    foreign_domain = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "synthetic-speaker_announce_send",
        config_entry=entry,
        suggested_object_id="unrelated_sensor",
    ).entity_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert all(registry.async_get(entity_id) is None for entity_id in retired)
    assert all(registry.async_get(entity_id) is not None for entity_id in retained)
    assert registry.async_get(foreign_platform) and registry.async_get(foreign_domain)
    other = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="other-speaker")
    other.add_to_hass(hass)
    other_button = registry.async_get_or_create(
        "button",
        DOMAIN,
        "synthetic-speaker_announce_send",
        config_entry=other,
        suggested_object_id="other_entry_button",
    ).entity_id
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(other_button) is not None
    for previous in texts:
        current = registry.async_get(previous.entity_id)
        assert current.unique_id == previous.unique_id
        assert current.name == "My custom control" and current.icon == "mdi:message-text"
        assert current.disabled_by == previous.disabled_by
        assert (
            registry.async_get_entity_id("text", DOMAIN, previous.unique_id) == previous.entity_id
        )
    c = entry.runtime_data.coordinator
    with patch.object(c, "async_request_refresh", AsyncMock()) as refresh:
        for entity_id in retained[:3]:
            await hass.services.async_call(
                "button", "press", {"entity_id": entity_id}, blocking=True
            )
        assert refresh.call_count == 2
    assert entry.options == {}
    assert await hass.config_entries.async_unload(entry.entry_id)
