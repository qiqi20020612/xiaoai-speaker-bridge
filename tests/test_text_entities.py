"""Native text actions and narrowly scoped legacy send-button migration."""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.logbook.processor import EventProcessor
from homeassistant.components.recorder.db_schema import EventData, Events, EventTypes
from homeassistant.components.recorder.util import session_scope
from homeassistant.const import EVENT_LOGBOOK_ENTRY
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    mock_component,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)
from sqlalchemy import select

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.const import DOMAIN
from tests.test_capabilities import setup
from tests.test_homeassistant import DATA
from tests.test_homeassistant import mock_clients as mock_clients


async def set_text(hass, entity_id, value):
    await hass.services.async_call(
        "text", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    """Prepare HA's native Recorder database before the hass fixture starts."""


@pytest.fixture
def logbook_config():
    return {}


@pytest.fixture
async def logbook_events(recorder_mock, recorder_config, logbook_config, hass):
    # UI assets are not needed; keep the native Logbook service and Recorder.
    mock_component(hass, "frontend")
    assert await async_setup_component(
        hass, "logbook", {"recorder": recorder_config or {}, "logbook": logbook_config}
    )
    return async_capture_events(hass, EVENT_LOGBOOK_ENTRY)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_clears_and_logs_each_full_successful_input(
    logbook_events, hass, mock_clients, kind
):
    entry, c = await setup(hass)
    registry = er.async_get(hass)
    text_id = registry.async_get_entity_id("text", DOMAIN, f"synthetic-speaker_{kind}_text")
    other = "send_text" if kind == "announce" else "announce"
    other_id = registry.async_get_entity_id("text", DOMAIN, f"synthetic-speaker_{other}_text")
    with patch.object(c.client, "submit", AsyncMock(return_value={"status": "completed"})):
        await set_text(hass, other_id, "另一实体的已提交文字")
        async with asyncio.timeout(2):
            await c.jobs.jobs[c.quick.requests[other]].done.wait()
        assert hass.states.get(other_id).state == ""
        values = ('  第一行：完整文字\n第二行：引号"、emoji 👋 {{ 1 + 1 }}  ', "字" * 255)
        for value in (*values, values[0]):
            context = Context()
            await hass.services.async_call(
                "text",
                "set_value",
                {"entity_id": text_id, "value": value},
                blocking=True,
                context=context,
            )
            assert hass.states.get(text_id).state == ""
            event = logbook_events[-1]
            assert event.context is context
            assert event.data == {
                "name": hass.states.get(text_id).name,
                "message": value,
                "entity_id": text_id,
                "domain": "text",
            }
            async with asyncio.timeout(2):
                await c.jobs.jobs[c.quick.requests[kind]].done.wait()
            c.async_set_updated_data(dict(c.data))
            assert hass.states.get(text_id).state == ""
            assert hass.states.get(text_id).attributes["status"] == "completed"
            assert hass.states.get(other_id).state == ""
    assert [(event.data["entity_id"], event.data["message"]) for event in logbook_events] == [
        (other_id, "另一实体的已提交文字"),
        *((text_id, value) for value in (*values, values[0])),
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_submission_failure_keeps_unqueued_input_without_logbook_entry(
    logbook_events, hass, mock_clients, kind
):
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
        assert hass.states.get(text_id).state == "被拒绝的新文字"
        assert hass.states.get(text_id).attributes["request_id"] == request_id
        assert [event.data["message"] for event in logbook_events] == [previous]
        assert not c.quick.pending
        with pytest.raises(HomeAssistantError, match="invalid_text"):
            await set_text(hass, text_id, " \n\t")
        assert hass.states.get(text_id).state == "被拒绝的新文字"
        assert [event.data["message"] for event in logbook_events] == [previous]
        await set_text(hass, text_id, "被拒绝的新文字")
        assert hass.states.get(text_id).state == ""
        assert [event.data["message"] for event in logbook_events] == [previous, "被拒绝的新文字"]
        async with asyncio.timeout(2):
            await c.jobs.jobs[c.quick.requests[kind]].done.wait()
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
@pytest.mark.parametrize("value", ["", " \n\t", "nul\0text", "x" * 256, None, 123])
async def test_text_rejects_invalid_input_without_submission(
    logbook_events, hass, mock_clients, kind, value
):
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
    assert not logbook_events
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
@pytest.mark.parametrize("unavailable", ["offline", "capability", "stale"])
async def test_text_rejects_unavailable_device_or_capability(
    logbook_events, hass, mock_clients, caplog, kind, unavailable
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
    assert not c.quick.pending and not logbook_events
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_rejects_concurrent_submission_before_service_returns(
    logbook_events, hass, mock_clients, kind
):
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
    assert hass.states.get(text_id).state == "" and not c.quick.pending
    assert [event.data["message"] for event in logbook_events] == ["first instruction"]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_keeps_service_errors_and_task_failure_feedback(
    logbook_events, hass, mock_clients, kind
):
    entry, c = await setup(hass, {"agent_id": "conversation.existing"})
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )
    with patch.object(c.jobs, "submit", AsyncMock(side_effect=BridgeError("queue_full"))):
        with pytest.raises(HomeAssistantError, match="queue_full"):
            await set_text(hass, text_id, "test")
    assert not c.quick.pending and kind not in c.quick.requests
    assert hass.states.get(text_id).state == "test"
    assert not logbook_events
    with patch.object(c.client, "submit", AsyncMock(side_effect=BridgeError("synthetic_failure"))):
        await set_text(hass, text_id, "test")
        request_id = c.quick.requests[kind]
        async with asyncio.timeout(2):
            await c.jobs.jobs[request_id].done.wait()
    state = hass.states.get(text_id)
    assert state.attributes["status"] == "failed"
    assert state.attributes["error"] == "synthetic_failure"
    assert state.attributes["request_id"] == request_id and state.state == ""
    assert [event.data["message"] for event in logbook_events] == ["test"]
    assert "agent_id" not in entry.options
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
@pytest.mark.parametrize(
    ("recorder_config", "logbook_config", "recorded", "visible"),
    [
        ({"purge_keep_days": 3}, {}, True, True),
        (
            {"purge_keep_days": 3, "exclude": {"entities": ["text.custom_instruction"]}},
            {},
            False,
            False,
        ),
        (
            {"purge_keep_days": 3},
            {"exclude": {"entities": ["text.custom_instruction"]}},
            True,
            False,
        ),
    ],
    ids=["native-history", "recorder-excluded", "logbook-excluded"],
)
async def test_text_history_uses_native_recorder_logbook_filters_and_renamed_entity(
    logbook_events, hass, mock_clients, recorder_mock, kind, recorded, visible
):
    entry, c = await setup(hass)
    registry = er.async_get(hass)
    unique_id = f"synthetic-speaker_{kind}_text"
    previous_id = registry.async_get_entity_id("text", DOMAIN, unique_id)
    count = len(registry.entities)
    text_id = "text.custom_instruction"
    registry.async_update_entity(previous_id, new_entity_id=text_id, name="Custom input")
    await hass.async_block_till_done()
    assert len(registry.entities) == count
    assert registry.async_get_entity_id("text", DOMAIN, unique_id) == text_id
    await hass.async_start()
    value = '  完整原文\n"quoted" 👋 {{ 1 + 1 }}  '
    value += "字" * (255 - len(value))
    start = dt_util.utcnow() - timedelta(seconds=1)
    with patch.object(c.client, "submit", AsyncMock(return_value={"status": "completed"})):
        await set_text(hass, text_id, value)
        async with asyncio.timeout(2):
            await c.jobs.jobs[c.quick.requests[kind]].done.wait()
    assert hass.states.get(text_id).state == ""
    assert logbook_events[-1].data["entity_id"] == text_id
    assert logbook_events[-1].data["message"] == value
    assert logbook_events[-1].data["name"] == hass.states.get(text_id).name
    await hass.async_block_till_done()
    await async_recorder_block_till_done(hass)
    entity_processor = EventProcessor(hass, (EVENT_LOGBOOK_ENTRY,), entity_ids=[text_id])
    activity_processor = EventProcessor(hass, (EVENT_LOGBOOK_ENTRY,))

    def read_native_history():
        with session_scope(hass=hass, read_only=True) as session:
            entries = session.scalars(
                select(EventData.shared_data)
                .join(Events, Events.data_id == EventData.data_id)
                .join(EventTypes, EventTypes.event_type_id == Events.event_type_id)
                .where(EventTypes.event_type == EVENT_LOGBOOK_ENTRY)
            ).all()
        end = dt_util.utcnow() + timedelta(seconds=1)
        return (
            [json.loads(data) for data in entries],
            entity_processor.get_events(start, end),
            activity_processor.get_events(start, end),
        )

    stored, entity_activity, activity = await hass.async_add_executor_job(read_native_history)
    expected = [value] if recorded else []
    assert [data["message"] for data in stored if data.get("entity_id") == text_id] == expected
    # HA's explicit entity query uses Recorder contents; the global Activity
    # query additionally applies Logbook's display filter. Preserve both rules.
    assert [data["message"] for data in entity_activity if data.get("entity_id") == text_id] == (
        expected
    )
    assert [data["message"] for data in activity if data.get("entity_id") == text_id] == (
        [value] if visible else []
    )
    assert recorder_mock.keep_days == 3
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_logbook_failure_cannot_fail_or_repeat_an_accepted_task(
    logbook_events, hass, mock_clients, caplog, kind
):
    entry, c = await setup(hass)
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )

    async def fail_log(call):
        raise HomeAssistantError("synthetic_logbook_failure")

    hass.services.async_register("logbook", "log", fail_log)
    with (
        patch.object(c.jobs, "submit", wraps=c.jobs.submit) as submit,
        patch.object(c.client, "submit", AsyncMock(return_value={"status": "completed"})),
    ):
        await set_text(hass, text_id, "synthetic-private-input")
        request_id = c.quick.requests[kind]
        async with asyncio.timeout(2):
            await c.jobs.jobs[request_id].done.wait()
        assert submit.call_count == 1
    assert not logbook_events and not c.quick.pending
    assert hass.states.get(text_id).state == ""
    assert hass.states.get(text_id).attributes["status"] == "completed"
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.xiaoai_speaker.quick"
    ]
    assert messages == [f"Task {request_id} queued, but its Logbook entry failed"]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_stays_busy_until_its_logbook_submission_finishes(
    logbook_events, hass, mock_clients, kind
):
    entry, c = await setup(hass)
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original_log = hass.services.async_services()["logbook"]["log"]

    async def delayed_log(call):
        entered.set()
        await release.wait()
        original_log.job.target(call)

    hass.services.async_register("logbook", "log", delayed_log, schema=original_log.schema)
    with patch.object(c.client, "submit", AsyncMock(return_value={"status": "completed"})):
        first = asyncio.create_task(set_text(hass, text_id, "first instruction"))
        try:
            async with asyncio.timeout(2):
                await entered.wait()
                await c.jobs.jobs[c.quick.requests[kind]].done.wait()
            with pytest.raises(HomeAssistantError, match="quick_action_busy"):
                await set_text(hass, text_id, "second instruction")
        finally:
            release.set()
            await first
    assert [event.data["message"] for event in logbook_events] == ["first instruction"]
    assert len(c.jobs.jobs) == 1 and not c.quick.pending
    assert hass.states.get(text_id).state == ""
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_text_without_logbook_keeps_existing_submission_available(hass, mock_clients, kind):
    entry, c = await setup(hass)
    assert not hass.services.has_service("logbook", "log")
    text_id = er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"synthetic-speaker_{kind}_text"
    )
    with (
        patch.object(c.jobs, "submit", wraps=c.jobs.submit) as submit,
        patch.object(c.client, "submit", AsyncMock(return_value={"status": "completed"})),
    ):
        await set_text(hass, text_id, "synthetic instruction")
        async with asyncio.timeout(2):
            await c.jobs.jobs[c.quick.requests[kind]].done.wait()
        assert submit.call_count == 1
    assert hass.states.get(text_id).state == "" and not c.quick.pending
    assert "logbook" not in hass.config.components
    assert "recorder" not in hass.config.components
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
