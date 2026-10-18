"""Native HA volume controls display intent without replaying device writes."""

import asyncio
import copy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.const import DOMAIN
from tests.test_homeassistant import DATA, INFO, STATE
from tests.test_homeassistant import mock_clients as mock_clients


def device_state(updated=100, **updates):
    return (
        copy.deepcopy(STATE)
        | {
            "instance_id": "volume-display-instance",
            "updated": updated,
            "state_stale": False,
            "audio_active": False,
            "volume_recovery_pending": False,
            "airplay": {"active": False, "state": "stopped"},
        }
        | updates
    )


@pytest.fixture
async def volume_entities(hass, mock_clients):
    originals = copy.deepcopy((INFO, STATE))
    sample = device_state()
    entry = MockConfigEntry(
        domain=DOMAIN, title="Synthetic speaker", data=DATA, unique_id="synthetic-speaker"
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "number",
        DOMAIN,
        "synthetic-speaker_volume_bluetooth",
        suggested_object_id="my_bluetooth_volume",
        config_entry=entry,
        disabled_by=None,
    )
    with (
        patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.info",
            AsyncMock(return_value=copy.deepcopy(INFO)),
        ),
        patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.state",
            AsyncMock(side_effect=lambda: copy.deepcopy(sample)),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        c = entry.runtime_data.coordinator
        await c.volume.commit(
            [{"target": channel, "level": level} for channel, level in sample["volumes"].items()]
        )
        numbers = {
            channel: registry.async_get_entity_id(
                "number", DOMAIN, f"synthetic-speaker_volume_{channel}"
            )
            for channel in ("master", "tts", "bluetooth")
        }
        player = registry.async_get_entity_id("media_player", DOMAIN, "synthetic-speaker_player")
        try:
            yield c, numbers, player, sample
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert (INFO, STATE) == originals


async def set_volume(hass, entrypoint, entity_id, level):
    return await hass.services.async_call(
        entrypoint,
        "set_value" if entrypoint == "number" else "volume_set",
        {
            "entity_id": entity_id,
            **({"value": level} if entrypoint == "number" else {"volume_level": level / 100}),
        },
        blocking=True,
    )


def displayed(hass, entrypoint, entity_id):
    state = hass.states.get(entity_id)
    return float(state.state) if entrypoint == "number" else state.attributes["volume_level"] * 100


def acknowledgement(target, level, request_id):
    return {"target": target, "level": level, "applied": True, "deferred": False}


async def commit_text_volume(client, plan):
    return await client.post(
        "/api/xiaoai_speaker/synthetic-speaker/volume_plan",
        json={"commit": True, "plan": plan},
    )


async def refresh(c, sample, **updates):
    sample.update(updates)
    await c.async_refresh()


async def wait_value(hass, entrypoint, entity_id, level):
    ready = asyncio.Event()

    @callback
    def changed(event):
        if event.data["entity_id"] == entity_id and displayed(hass, entrypoint, entity_id) == level:
            ready.set()

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, changed)
    try:
        if displayed(hass, entrypoint, entity_id) != level:
            async with asyncio.timeout(5):
                await ready.wait()
    finally:
        unsubscribe()


@pytest.mark.parametrize(
    "entrypoint,channel",
    [
        ("number", "master"),
        ("number", "tts"),
        ("number", "bluetooth"),
        ("media_player", "master"),
    ],
)
async def test_target_is_visible_before_http_returns(hass, volume_entities, entrypoint, channel):
    c, numbers, player, sample = volume_entities
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(target, level, request_id):
        entered.set()
        await release.wait()
        return {"target": target, "level": level, "applied": True, "deferred": False}

    entity_id = numbers[channel] if entrypoint == "number" else player
    with patch.object(c.client, "volume", side_effect=write) as control:
        task = asyncio.create_task(set_volume(hass, entrypoint, entity_id, 75))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            assert displayed(hass, entrypoint, entity_id) == 75
            assert c.volume.desired.get(channel) != 75
            assert c.data["volumes"][channel] == sample["volumes"][channel]
            if channel == "master":
                assert displayed(hass, "number", numbers["master"]) == 75
                assert displayed(hass, "media_player", player) == 75
            release.set()
            await task
            assert displayed(hass, entrypoint, entity_id) == 75
            assert c.volume.desired[channel] == 75
            assert control.await_count == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "entrypoint,channel",
    [
        ("number", "master"),
        ("number", "tts"),
        ("number", "bluetooth"),
        ("media_player", "master"),
    ],
)
@pytest.mark.parametrize(
    "code,uncertain",
    [
        ("hardware_control_failed", False),
        ("busy", False),
        ("connection_failed", True),
        ("auth_failed", False),
    ],
)
async def test_failed_request_restores_prior_value_and_reports_error(
    hass, volume_entities, entrypoint, channel, code, uncertain
):
    c, numbers, player, sample = volume_entities
    before = copy.deepcopy(c.volume.desired)
    entity_id = numbers[channel] if entrypoint == "number" else player
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*args):
        entered.set()
        await release.wait()
        raise BridgeError(code, uncertain=uncertain)

    with patch.object(c.client, "volume", side_effect=fail) as control:
        task = asyncio.create_task(set_volume(hass, entrypoint, entity_id, 75))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            assert displayed(hass, entrypoint, entity_id) == 75
            release.set()
            with pytest.raises(HomeAssistantError, match=code):
                await task
            assert displayed(hass, entrypoint, entity_id) == before[channel]
            assert c.volume.desired == before
            assert c.data["volumes"] == sample["volumes"]
            assert control.await_count == 1  # No rollback write or ambiguous retry.
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("entrypoint", ["number", "media_player"])
async def test_success_waits_for_new_safe_probe_without_refresh_or_control_replay(
    hass, volume_entities, entrypoint
):
    c, numbers, player, sample = volume_entities
    entity_id = numbers["master"] if entrypoint == "number" else player
    with (
        patch.object(c.client, "volume", side_effect=acknowledgement) as control,
        patch.object(c, "async_request_refresh", AsyncMock()) as immediate_refresh,
    ):
        await set_volume(hass, entrypoint, entity_id, 75)
        assert displayed(hass, entrypoint, entity_id) == 75
        immediate_refresh.assert_not_awaited()
        await refresh(c, sample)  # Same old cached Probe after acknowledgement.
        await refresh(c, sample)
        assert displayed(hass, entrypoint, entity_id) == 75
        assert c.volume.desired["master"] == 75
        assert c.data["volumes"]["master"] == 60
        # Probe can sample before the write and finish after it. Its completion
        # timestamp alone does not prove a post-write mixer observation.
        await refresh(c, sample, updated=101)
        assert displayed(hass, entrypoint, entity_id) == 75
        assert c.volume.desired["master"] == 75
        await refresh(c, sample, updated=100)  # Out-of-order cache, not a new sample.
        assert displayed(hass, entrypoint, entity_id) == 75
        await refresh(c, sample, updated=102, volumes={**sample["volumes"], "master": 74})
        assert displayed(hass, entrypoint, entity_id) == 74
        assert c.volume.desired["master"] == 74
        # Normal external changes resume after the optimistic value is resolved.
        await refresh(c, sample, updated=103, volumes={**sample["volumes"], "master": 45})
        assert displayed(hass, entrypoint, entity_id) == 45
        assert c.volume.desired["master"] == 45
        assert control.await_count == 1


@pytest.mark.parametrize("bad", [None, True, "10", -1, 101, float("nan"), float("inf")])
async def test_invalid_or_null_probe_cannot_replace_target_or_last_actual(
    hass, volume_entities, bad
):
    c, numbers, player, sample = volume_entities
    with patch.object(c.client, "volume", side_effect=acknowledgement) as control:
        await set_volume(hass, "number", numbers["master"], 75)
        await refresh(c, sample, updated=101, volumes={**sample["volumes"], "master": bad})
        assert displayed(hass, "number", numbers["master"]) == 75
        assert displayed(hass, "media_player", player) == 75
        assert c.volume.desired["master"] == 75
        await refresh(c, sample, updated=102, volumes={**sample["volumes"], "master": 75})
        assert displayed(hass, "number", numbers["master"]) == 75
        await refresh(c, sample, updated=103, volumes={**sample["volumes"], "master": bad})
        assert displayed(hass, "number", numbers["master"]) == 75
        assert c.volume.desired["master"] == 75
        assert control.await_count == 1


@pytest.mark.parametrize("channel", ["master", "tts", "bluetooth"])
@pytest.mark.parametrize("temporary", ["voice", "duck", "tts"])
async def test_temporary_attenuation_and_deferred_ack_preserve_user_target(
    hass, volume_entities, channel, temporary
):
    c, numbers, player, sample = volume_entities
    original = copy.deepcopy(sample["volumes"])
    flags = {
        "voice": {"voice": {"active": True, "volumes": original}},
        "duck": {"volume_recovery_pending": True},
        "tts": {"audio_active": True, "volume_recovery_pending": True},
    }[temporary]
    await refresh(c, sample, updated=101, volumes={**original, channel: 10}, **flags)
    assert displayed(hass, "number", numbers[channel]) == original[channel]
    with patch.object(
        c.client,
        "volume",
        AsyncMock(
            return_value={
                "target": channel,
                "level": 75,
                "applied": False,
                "deferred": True,
            }
        ),
    ) as control:
        await set_volume(hass, "number", numbers[channel], 75)
        await refresh(c, sample, updated=102)
        assert displayed(hass, "number", numbers[channel]) == 75
        assert c.volume.desired[channel] == 75
        # Dynamic voice/audio flags can be idle while the cached mixer sample
        # still belongs to a retained recovery Journal.
        await refresh(
            c,
            sample,
            updated=103,
            voice={"active": False},
            audio_active=False,
            volume_recovery_pending=True,
        )
        assert displayed(hass, "number", numbers[channel]) == 75
        await refresh(
            c, sample, updated=104, volume_recovery_pending=False, volumes={**original, channel: 75}
        )
        assert displayed(hass, "number", numbers[channel]) == 75
        await refresh(c, sample, updated=105, volumes={**original, channel: 45})
        assert displayed(hass, "number", numbers[channel]) == 45
        assert c.volume.desired[channel] == 45
        assert control.await_count == 1


@pytest.mark.parametrize("first_fails", [False, True])
@pytest.mark.parametrize("second_fails", [False, True])
async def test_continuous_number_and_player_changes_keep_newest_intent(
    hass, volume_entities, first_fails, second_fails
):
    c, numbers, player, sample = volume_entities
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = []

    async def write(target, level, request_id):
        index = len(calls)
        calls.append((target, level, request_id))
        entered[index].set()
        await release[index].wait()
        if (first_fails, second_fails)[index]:
            raise BridgeError("hardware_control_failed")
        return acknowledgement(target, level, request_id)

    with patch.object(c.client, "volume", side_effect=write):
        first = asyncio.create_task(set_volume(hass, "number", numbers["master"], 70))
        second = None
        try:
            async with asyncio.timeout(5):
                await entered[0].wait()
            second = asyncio.create_task(set_volume(hass, "media_player", player, 80))
            await wait_value(hass, "media_player", player, 80)
            assert displayed(hass, "number", numbers["master"]) == 80
            assert len(calls) == 1  # The newer display does not reorder writes.
            release[0].set()
            async with asyncio.timeout(5):
                await entered[1].wait()
            assert displayed(hass, "number", numbers["master"]) == 80
            first_result = (await asyncio.gather(first, return_exceptions=True))[0]
            assert isinstance(first_result, HomeAssistantError) is first_fails
            release[1].set()
            second_result = (await asyncio.gather(second, return_exceptions=True))[0]
            assert isinstance(second_result, HomeAssistantError) is second_fails
            expected = 80 if not second_fails else (60 if first_fails else 70)
            assert displayed(hass, "number", numbers["master"]) == expected
            assert displayed(hass, "media_player", player) == expected
            assert c.volume.desired["master"] == expected
            assert [(target, level) for target, level, _ in calls] == [
                ("master", 70),
                ("master", 80),
            ]
            assert calls[0][2] != calls[1][2]
            # A later failure must retain the earlier success's protection from
            # old cached samples, not just restore its value for one UI frame.
            await refresh(c, sample)
            await refresh(c, sample, updated=101)
            assert displayed(hass, "number", numbers["master"]) == expected
            await refresh(c, sample, updated=102, volumes={**sample["volumes"], "master": expected})
            assert c.volume.desired["master"] == expected
        finally:
            for event in release:
                event.set()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None), return_exceptions=True
            )


@pytest.mark.parametrize("poll_started_first", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_old_get_response_cannot_undo_control_or_rollback(
    hass, volume_entities, poll_started_first, write_fails
):
    c, numbers, _, sample = volume_entities
    poll_entered, poll_release = asyncio.Event(), asyncio.Event()
    write_entered, write_release = asyncio.Event(), asyncio.Event()
    old = device_state(updated=99, volumes={**sample["volumes"], "master": 45})

    async def read():
        poll_entered.set()
        await poll_release.wait()
        return copy.deepcopy(old)

    async def write(target, level, request_id):
        write_entered.set()
        await write_release.wait()
        if write_fails:
            raise BridgeError("connection_failed", uncertain=True)
        return acknowledgement(target, level, request_id)

    operation = poll = None
    with (
        patch.object(c.client, "state", side_effect=read),
        patch.object(c.client, "volume", side_effect=write) as control,
    ):
        try:
            if poll_started_first:
                poll = asyncio.create_task(c.async_refresh())
                async with asyncio.timeout(5):
                    await poll_entered.wait()
            operation = asyncio.create_task(set_volume(hass, "number", numbers["master"], 75))
            async with asyncio.timeout(5):
                await write_entered.wait()
            if not poll_started_first:
                poll = asyncio.create_task(c.async_refresh())
                async with asyncio.timeout(5):
                    await poll_entered.wait()
            write_release.set()
            result = (await asyncio.gather(operation, return_exceptions=True))[0]
            assert isinstance(result, HomeAssistantError) is write_fails
            poll_release.set()
            await poll
            expected = 60 if write_fails else 75
            assert displayed(hass, "number", numbers["master"]) == expected
            assert c.volume.desired["master"] == expected
            assert control.await_count == 1
        finally:
            poll_release.set()
            write_release.set()
            await asyncio.gather(
                *(task for task in (operation, poll) if task is not None), return_exceptions=True
            )


@pytest.mark.parametrize(
    "source,target,initial", [("bluetooth", "bluetooth", 40), ("airplay", "airplay", 35)]
)
async def test_player_optimism_stays_on_its_routed_channel(
    hass, volume_entities, source, target, initial
):
    c, numbers, player, sample = volume_entities
    caps = copy.deepcopy(INFO["capabilities"])
    caps["volume"][target] = True
    await refresh(
        c,
        sample,
        updated=101,
        capabilities=caps,
        volumes={**sample["volumes"], target: initial},
        **{source: {"active": True, "state": "playing"}},
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(channel, level, request_id):
        assert channel == target
        entered.set()
        await release.wait()
        return acknowledgement(channel, level, request_id)

    with patch.object(c.client, "volume", side_effect=write) as control:
        operation = asyncio.create_task(set_volume(hass, "media_player", player, 75))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            assert displayed(hass, "media_player", player) == 75
            assert displayed(hass, "number", numbers["master"]) == 60
            if target == "bluetooth":
                assert displayed(hass, "number", numbers["bluetooth"]) == 75
            release.set()
            await operation
            assert c.volume.desired[target] == 75
            assert control.await_count == 1
        finally:
            release.set()
            await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("channel", ["master", "tts"])
async def test_storage_failure_rolls_back_display_and_does_not_replay_write(
    hass, volume_entities, channel
):
    c, numbers, _, _ = volume_entities
    before = copy.deepcopy(c.volume.desired)
    with (
        patch.object(c.client, "volume", side_effect=acknowledgement) as control,
        patch.object(
            c.volume.store,
            "async_save",
            AsyncMock(side_effect=OSError("synthetic storage failure")),
        ),
    ):
        with pytest.raises(HomeAssistantError, match="state_write_failed"):
            await set_volume(hass, "number", numbers[channel], 75)
        assert displayed(hass, "number", numbers[channel]) == before[channel]
        assert c.volume.desired == before
        assert control.await_count == 1


async def test_partial_all_failure_keeps_only_acknowledged_channels(hass, volume_entities):
    c, numbers, player, _ = volume_entities
    calls = []

    async def write(target, level, request_id):
        calls.append(target)
        if target == "tts":
            raise BridgeError("hardware_control_failed")
        return acknowledgement(target, level, request_id)

    with patch.object(c.client, "volume", side_effect=write):
        with pytest.raises(HomeAssistantError, match="hardware_control_failed"):
            await hass.services.async_call(
                DOMAIN,
                "set_volume",
                {"entity_id": player, "target": "all", "level": 75},
                blocking=True,
            )
    assert calls == ["master", "tts"]
    assert c.volume.desired == {"master": 75, "tts": 55, "bluetooth": 40}
    assert displayed(hass, "number", numbers["master"]) == 75
    assert displayed(hass, "number", numbers["tts"]) == 55
    assert displayed(hass, "number", numbers["bluetooth"]) == 40


async def test_stale_poll_waiting_for_storage_cannot_persist_over_new_target(hass, volume_entities):
    c, numbers, _, sample = volume_entities
    original_commit = c.volume.commit
    entered = asyncio.Event()

    async def commit(plan, **kwargs):
        if kwargs:
            entered.set()
        return await original_commit(plan, **kwargs)

    await c.volume._store_lock.acquire()
    held = True
    poll = operation = None
    try:
        with (
            patch.object(c.volume, "commit", side_effect=commit),
            patch.object(c.volume.store, "async_save", wraps=c.volume.store.async_save) as save,
            patch.object(c.client, "volume", side_effect=acknowledgement) as control,
        ):
            sample["volumes"]["master"] = 45
            poll = asyncio.create_task(c.async_refresh())
            async with asyncio.timeout(5):
                await entered.wait()
            operation = asyncio.create_task(set_volume(hass, "number", numbers["master"], 75))
            await wait_value(hass, "number", numbers["master"], 75)
            c.volume._store_lock.release()
            held = False
            await asyncio.gather(poll, operation)
            assert c.volume.desired["master"] == 75
            assert displayed(hass, "number", numbers["master"]) == 75
            assert [call.args[0]["master"] for call in save.await_args_list] == [75]
            assert control.await_count == 1
    finally:
        if held:
            c.volume._store_lock.release()
        await asyncio.gather(
            *(task for task in (poll, operation) if task is not None), return_exceptions=True
        )


async def test_missing_probe_identity_or_timestamp_cannot_confirm_target(hass, volume_entities):
    c, numbers, _, sample = volume_entities
    with patch.object(c.client, "volume", side_effect=acknowledgement) as control:
        await set_volume(hass, "number", numbers["master"], 75)
        for updated in (None, True, "101"):
            await refresh(c, sample, updated=updated)
            assert displayed(hass, "number", numbers["master"]) == 75
        await refresh(c, sample, updated=101, instance_id=None)
        assert displayed(hass, "number", numbers["master"]) == 75
        await refresh(c, sample, updated=102, instance_id="volume-display-instance")
        await refresh(c, sample, updated=103, volumes={**sample["volumes"], "master": 75})
        await refresh(c, sample, updated=104, volumes={**sample["volumes"], "master": 45})
        assert displayed(hass, "number", numbers["master"]) == 45
        assert c.volume.desired["master"] == 45
        assert control.await_count == 1


async def test_stale_and_failed_polls_keep_intent_without_hiding_unavailability(
    hass, volume_entities
):
    c, numbers, _, sample = volume_entities
    with patch.object(c.client, "volume", side_effect=acknowledgement) as control:
        await set_volume(hass, "number", numbers["master"], 75)
        await refresh(c, sample, updated=101, state_stale=True)
        assert hass.states.get(numbers["master"]).state == "unavailable"
        assert c.volume.desired["master"] == 75
        with patch.object(c.client, "state", side_effect=BridgeError("connection_failed")):
            await c.async_refresh()
            assert hass.states.get(numbers["master"]).state == "unavailable"
        await refresh(
            c, sample, updated=102, state_stale=False, volumes={**sample["volumes"], "master": 75}
        )
        assert displayed(hass, "number", numbers["master"]) == 75
        await refresh(c, sample, updated=103, volumes={**sample["volumes"], "master": 45})
        assert displayed(hass, "number", numbers["master"]) == 45
        assert control.await_count == 1


@pytest.mark.parametrize("entrypoint", ["media_player", DOMAIN])
@pytest.mark.parametrize(
    "service,first,expected",
    [("volume_up", 70, 80), ("volume_down", 30, 20), ("volume_up", 95, 100), ("volume_down", 5, 0)],
)
async def test_relative_controls_use_latest_pending_target(
    hass, volume_entities, entrypoint, service, first, expected
):
    c, numbers, player, _ = volume_entities
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def write(target, level, request_id):
        calls.append((target, level))
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return acknowledgement(target, level, request_id)

    second = None
    with patch.object(c.client, "volume", side_effect=write):
        operation = asyncio.create_task(set_volume(hass, "number", numbers["master"], first))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            second = asyncio.create_task(
                hass.services.async_call(
                    entrypoint,
                    service,
                    {"entity_id": player},
                    blocking=True,
                )
            )
            await wait_value(hass, "number", numbers["master"], expected)
            assert calls == [("master", first)]
            release.set()
            await asyncio.gather(operation, second)
            assert c.volume.desired["master"] == expected
            assert displayed(hass, "media_player", player) == expected
            assert calls == [("master", first), ("master", expected)]
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (operation, second) if task is not None), return_exceptions=True
            )


async def test_failure_on_one_channel_cannot_rollback_another_channel(hass, volume_entities):
    c, numbers, _, _ = volume_entities
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def write(target, level, request_id):
        calls.append((target, level))
        if target == "master":
            entered.set()
            await release.wait()
            raise BridgeError("hardware_control_failed")
        return acknowledgement(target, level, request_id)

    bluetooth = None
    with patch.object(c.client, "volume", side_effect=write):
        master = asyncio.create_task(set_volume(hass, "number", numbers["master"], 70))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            bluetooth = asyncio.create_task(set_volume(hass, "number", numbers["bluetooth"], 80))
            await wait_value(hass, "number", numbers["bluetooth"], 80)
            assert displayed(hass, "number", numbers["master"]) == 70
            release.set()
            results = await asyncio.gather(master, bluetooth, return_exceptions=True)
            assert isinstance(results[0], HomeAssistantError)
            assert results[1] is None
            assert displayed(hass, "number", numbers["master"]) == 60
            assert displayed(hass, "number", numbers["bluetooth"]) == 80
            assert c.volume.desired == {"master": 60, "tts": 55, "bluetooth": 80}
            assert calls == [("master", 70), ("bluetooth", 80)]
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (master, bluetooth) if task is not None), return_exceptions=True
            )


@pytest.mark.parametrize("replay_first", [False, True])
async def test_restart_replay_is_ordered_with_controls_and_reads_latest_desired(
    hass, volume_entities, replay_first
):
    c, numbers, _, sample = volume_entities
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def write(target, level, request_id):
        calls.append((target, level))
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return acknowledgement(target, level, request_id)

    read_entered = asyncio.Event()

    async def read():
        read_entered.set()
        return copy.deepcopy(sample)

    poll = operation = None
    with (
        patch.object(c.client, "volume", side_effect=write),
        patch.object(c.client, "state", side_effect=read),
    ):
        sample["instance_id"] = "restarted-volume-display-instance"
        try:
            if replay_first:
                poll = asyncio.create_task(c.async_refresh())
            else:
                operation = asyncio.create_task(set_volume(hass, "number", numbers["master"], 75))
            async with asyncio.timeout(5):
                await entered.wait()
            if replay_first:
                operation = asyncio.create_task(set_volume(hass, "number", numbers["master"], 75))
            else:
                poll = asyncio.create_task(c.async_refresh())
                async with asyncio.timeout(5):
                    await read_entered.wait()
            await wait_value(hass, "number", numbers["master"], 75)
            assert calls == [("master", 60 if replay_first else 75)]
            release.set()
            await asyncio.gather(poll, operation)
            assert [(target, level) for target, level in calls if target == "master"] == [
                ("master", 60 if replay_first else 75),
                ("master", 75),
            ]
            assert c._synced_instance == sample["instance_id"]
            assert c.volume.desired["master"] == 75
            assert displayed(hass, "number", numbers["master"]) == 75
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (poll, operation) if task is not None), return_exceptions=True
            )


async def test_cancelled_waiter_does_not_cancel_earlier_control_or_send_a_write(
    hass, volume_entities
):
    c, numbers, player, sample = volume_entities
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(target, level, request_id):
        entered.set()
        await release.wait()
        return acknowledgement(target, level, request_id)

    second = None
    with patch.object(c.client, "volume", side_effect=write) as control:
        first = asyncio.create_task(c.volume.set("master", 70))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            second = asyncio.create_task(c.volume.set("master", 80))
            await wait_value(hass, "number", numbers["master"], 80)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert displayed(hass, "number", numbers["master"]) == 70
            assert displayed(hass, "media_player", player) == 70
            assert c.volume.desired["master"] == 60  # The first request is still in flight.
            release.set()
            await first
            assert displayed(hass, "number", numbers["master"]) == 70
            await refresh(c, sample)
            await refresh(c, sample, updated=101)
            assert displayed(hass, "number", numbers["master"]) == 70
            assert c.volume.desired["master"] == 70
            assert control.await_count == 1
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None), return_exceptions=True
            )


@pytest.mark.parametrize("entrypoint", ["media_player", DOMAIN])
async def test_relative_control_after_cancelling_newest_uses_still_pending_target(
    hass, volume_entities, entrypoint
):
    c, numbers, player, _ = volume_entities
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def write(target, level, request_id):
        calls.append((target, level))
        entered.set()
        await release.wait()
        return acknowledgement(target, level, request_id)

    second = relative = None
    with patch.object(c.client, "volume", side_effect=write):
        first = asyncio.create_task(c.volume.set("master", 70))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            second = asyncio.create_task(c.volume.set("master", 80))
            await wait_value(hass, "number", numbers["master"], 80)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            relative = asyncio.create_task(
                hass.services.async_call(
                    entrypoint, "volume_up", {"entity_id": player}, blocking=True
                )
            )
            await wait_value(hass, "number", numbers["master"], 80)
            assert calls == [("master", 70)]
            release.set()
            await asyncio.gather(first, relative)
            assert calls == [("master", 70), ("master", 80)]
            assert c.volume.desired["master"] == 80
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, second, relative) if task is not None),
                return_exceptions=True,
            )


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
async def test_cancelling_queued_requests_in_either_order_restores_live_predecessor(
    hass, volume_entities, order
):
    c, numbers, _, sample = volume_entities
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(target, level, request_id):
        entered.set()
        await release.wait()
        return acknowledgement(target, level, request_id)

    tasks = []
    levels = [70, 80, 90]
    with patch.object(c.client, "volume", side_effect=write) as control:
        try:
            tasks.append(asyncio.create_task(c.volume.set("master", levels[0])))
            async with asyncio.timeout(5):
                await entered.wait()
            for level in levels[1:]:
                tasks.append(asyncio.create_task(c.volume.set("master", level)))
                await wait_value(hass, "number", numbers["master"], level)
            cancelled = set()
            for index in order:
                tasks[index].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[index]
                cancelled.add(index)
                expected = next(levels[i] for i in (2, 1, 0) if i not in cancelled)
                assert displayed(hass, "number", numbers["master"]) == expected
                assert c.volume.desired["master"] == 60
            release.set()
            await tasks[0]
            await refresh(c, sample)
            await refresh(c, sample, updated=101, volumes={**sample["volumes"], "master": 70})
            await refresh(c, sample, updated=102, volumes={**sample["volumes"], "master": 45})
            assert displayed(hass, "number", numbers["master"]) == 45
            assert c.volume.desired["master"] == 45
            control.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("first_fails", [False, True])
async def test_cancelled_equal_level_request_cannot_leave_a_display_orphan(
    hass, volume_entities, first_fails
):
    c, numbers, _, sample = volume_entities
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(target, level, request_id):
        entered.set()
        await release.wait()
        if first_fails:
            raise BridgeError("hardware_control_failed")
        return acknowledgement(target, level, request_id)

    second = None
    with patch.object(c.client, "volume", side_effect=write) as control:
        first = asyncio.create_task(c.volume.set("master", 70))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            queued = asyncio.Event()

            @callback
            def changed():
                c.async_update_listeners()
                queued.set()

            # Equal targets do not fire a HA state change; wait for the existing
            # listener notification before cancelling the queued request.
            with patch.object(c.volume, "on_change", changed):
                second = asyncio.create_task(c.volume.set("master", 70))
                async with asyncio.timeout(5):
                    await queued.wait()
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert displayed(hass, "number", numbers["master"]) == 70
            release.set()
            result = (await asyncio.gather(first, return_exceptions=True))[0]
            assert isinstance(result, BridgeError) is first_fails
            expected = 60 if first_fails else 70
            assert displayed(hass, "number", numbers["master"]) == expected
            assert c.volume.desired["master"] == expected
            await refresh(c, sample)
            await refresh(c, sample, updated=101, volumes={**sample["volumes"], "master": 45})
            await refresh(c, sample, updated=102)
            assert displayed(hass, "number", numbers["master"]) == 45
            assert c.volume.desired["master"] == 45
            control.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None), return_exceptions=True
            )


@pytest.mark.parametrize(
    "channel,text",
    [("master", "主音量调到40%"), ("tts", "播报音量调到40%"), ("bluetooth", "蓝牙音量调到40%")],
)
@pytest.mark.parametrize("temporary", ["idle", "voice", "duck"])
async def test_text_success_replaces_older_target_and_safe_probe_boundary(
    hass, volume_entities, hass_client, channel, text, temporary
):
    c, numbers, player, sample = volume_entities
    client = await hass_client()
    with patch.object(c.client, "volume", side_effect=acknowledgement) as control:
        await set_volume(hass, "number", numbers[channel], 75)
        flags = {
            "idle": {},
            "voice": {"voice": {"active": True, "volumes": copy.deepcopy(sample["volumes"])}},
            "duck": {"audio_active": True, "volume_recovery_pending": True},
        }[temporary]
        await refresh(c, sample, updated=101, **flags)
        generation = c.volume.generation
        response = await client.post(
            "/api/xiaoai_speaker/synthetic-speaker/volume_plan", json={"text": text}
        )
        assert response.status == 200
        plan = (await response.json())["plan"]
        assert plan == [{"target": channel, "level": 40}]
        assert displayed(hass, "number", numbers[channel]) == 75  # Planning is not execution.
        assert c.volume.generation == generation
        response = await commit_text_volume(client, plan)
        assert response.status == 200 and await response.json() == {"committed": True}
        assert displayed(hass, "number", numbers[channel]) == 40
        if channel == "master":
            assert displayed(hass, "media_player", player) == 40
        assert c.volume.desired[channel] == 40
        assert c.volume.generation > generation
        await refresh(c, sample)  # The old cached sample must not undo the text command.
        await refresh(
            c,
            sample,
            updated=102,
            voice={"active": False},
            audio_active=False,
            volume_recovery_pending=False,
        )
        assert displayed(hass, "number", numbers[channel]) == 40
        assert c.volume.desired[channel] == 40
        await refresh(c, sample, updated=103, volumes={**sample["volumes"], channel: 40})
        await refresh(c, sample, updated=104, volumes={**sample["volumes"], channel: 45})
        assert displayed(hass, "number", numbers[channel]) == 45
        assert c.volume.desired[channel] == 45
        assert control.await_count == 1  # The callback never repeats the device write.


@pytest.mark.parametrize("entrypoint", ["media_player", DOMAIN])
async def test_relative_control_starts_from_latest_successful_text_commit(
    hass, volume_entities, hass_client, entrypoint
):
    c, numbers, player, _ = volume_entities
    client = await hass_client()
    with patch.object(c.client, "volume", side_effect=acknowledgement) as control:
        await set_volume(hass, "number", numbers["master"], 75)
        response = await commit_text_volume(client, [{"target": "master", "level": 40}])
        assert response.status == 200
        await hass.services.async_call(
            entrypoint, "volume_up", {"entity_id": player}, blocking=True
        )
        assert [call.args[:2] for call in control.await_args_list] == [
            ("master", 75),
            ("master", 50),
        ]
        assert displayed(hass, "number", numbers["master"]) == 50


@pytest.mark.parametrize("failure", ["invalid_volume", "state_write_failed"])
async def test_failed_text_commit_keeps_prior_display_and_generation(
    hass, volume_entities, hass_client, failure
):
    c, numbers, _, _ = volume_entities
    client = await hass_client()
    with patch.object(c.client, "volume", side_effect=acknowledgement) as control:
        await set_volume(hass, "number", numbers["master"], 75)
        generation = c.volume.generation
        with patch.object(
            c.volume.store,
            "async_save",
            AsyncMock(side_effect=OSError("synthetic storage failure")),
        ) as save:
            response = await commit_text_volume(
                client, [{"target": "master", "level": 101 if failure == "invalid_volume" else 40}]
            )
        assert response.status == 409 and await response.json() == {"error": failure}
        assert save.await_count == (failure == "state_write_failed")
        assert displayed(hass, "number", numbers["master"]) == 75
        assert c.volume.desired["master"] == 75
        assert c.volume.generation == generation
        assert control.await_count == 1


@pytest.mark.parametrize("write_fails", [False, True])
async def test_text_commit_preserves_pending_ha_intent_and_becomes_failure_fallback(
    hass, volume_entities, hass_client, write_fails
):
    c, numbers, player, sample = volume_entities
    client = await hass_client()
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(target, level, request_id):
        if level == 80:
            entered.set()
            await release.wait()
            if write_fails:
                raise BridgeError("hardware_control_failed")
        return acknowledgement(target, level, request_id)

    with patch.object(c.client, "volume", side_effect=write) as control:
        await set_volume(hass, "number", numbers["master"], 75)
        operation = asyncio.create_task(set_volume(hass, "number", numbers["master"], 80))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            response = await commit_text_volume(client, [{"target": "master", "level": 40}])
            assert response.status == 200
            assert c.volume.desired["master"] == 40
            assert displayed(hass, "number", numbers["master"]) == 80
            assert displayed(hass, "media_player", player) == 80
            release.set()
            result = (await asyncio.gather(operation, return_exceptions=True))[0]
            assert isinstance(result, HomeAssistantError) is write_fails
            expected = 40 if write_fails else 80
            assert displayed(hass, "number", numbers["master"]) == expected
            assert c.volume.desired["master"] == expected
            await refresh(c, sample)
            await refresh(c, sample, updated=101)
            assert displayed(hass, "number", numbers["master"]) == expected
            assert c.volume.desired["master"] == expected
            assert control.await_count == 2
        finally:
            release.set()
            await asyncio.gather(operation, return_exceptions=True)


async def test_poll_started_before_text_commit_cannot_override_it(
    hass, volume_entities, hass_client
):
    c, numbers, _, sample = volume_entities
    client = await hass_client()
    entered, release = asyncio.Event(), asyncio.Event()

    async def read():
        entered.set()
        await release.wait()
        return copy.deepcopy(sample) | {"updated": 101}

    with (
        patch.object(c.client, "volume", side_effect=acknowledgement) as control,
        patch.object(c.client, "state", side_effect=read),
    ):
        poll = asyncio.create_task(c.async_refresh())
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            response = await commit_text_volume(client, [{"target": "master", "level": 40}])
            assert response.status == 200
            release.set()
            await poll
            assert displayed(hass, "number", numbers["master"]) == 40
            assert c.volume.desired["master"] == 40
            control.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(poll, return_exceptions=True)
