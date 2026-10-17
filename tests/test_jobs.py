import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.jobs import JobManager


async def test_cancel_bridge_accepted_announcement_reaches_device(hass):
    client = AsyncMock()
    client.submit.return_value = {"status": "accepted"}
    client.job.return_value = {"status": "cancelled"}
    client.cancel.return_value = {"status": "cancelled"}
    accepted = asyncio.Event()
    manager = JobManager(hass, client)
    job = await manager.submit("announce", {"text": "synthetic speech"})
    manager.on_change = lambda: (
        accepted.set() if client.submit.await_count and job.status == "accepted" else None
    )
    manager.start()
    try:
        async with asyncio.timeout(3):
            await accepted.wait()
        assert manager.executing
        assert (await manager.cancel(job.request_id))["status"] == "cancelled"
        client.cancel.assert_awaited_once_with(job.request_id)
        assert (await manager.wait(job, 3))["status"] == "cancelled"
        assert client.submit.await_count == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
async def test_cancel_device_acknowledgement_updates_response_and_result(hass, status):
    client = AsyncMock()
    client.cancel.return_value = {
        "status": status,
        "reply_text": "confirmed reply",
        "error": "confirmed_error",
        "source": "airplay",
        "uncertain": False,
        "ignored": "not part of the job result",
    }
    manager = JobManager(hass, client)
    job = await manager.submit("announce", {"text": "synthetic speech"})
    job.dispatched = True
    job.result = {"error": "stale_error"}
    changed = []
    manager.on_change = lambda: changed.append((job.status, dict(job.result)))
    try:
        assert await manager.cancel(job.request_id) == {
            "request_id": job.request_id,
            "status": status,
        }
        expected = {
            "reply_text": "confirmed reply",
            "error": "confirmed_error",
            "source": "airplay",
            "uncertain": False,
        }
        assert job.status == status and job.result == expected
        assert changed == [(status, expected)]
        # Device acknowledgement does not claim that worker/resource cleanup ended.
        assert manager.executing and not job.done.is_set()
        client.cancel.assert_awaited_once_with(job.request_id)
    finally:
        await manager.close()


@pytest.mark.parametrize("stage", ["submit", "job"])
@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
async def test_cancel_confirmation_survives_older_inflight_response(hass, stage, error, status):
    entered = asyncio.Event()
    release = asyncio.Event()
    client = AsyncMock()

    async def old_response(*args):
        entered.set()
        await release.wait()
        if error:
            raise BridgeError("connection_failed", uncertain=True)
        return {"status": "accepted" if stage == "submit" else "playing"}

    client.submit.return_value = {"status": "accepted"}
    getattr(client, stage).side_effect = old_response
    client.cancel.return_value = {"status": status, "error": "confirmed_error"}
    manager = JobManager(hass, client)
    job = await manager.submit("send_text", {"text": "synthetic instruction"})
    manager.start()
    try:
        async with asyncio.timeout(3):
            await entered.wait()
        assert (await manager.cancel(job.request_id))["status"] == status
        assert job.status == status and job.result == {"error": "confirmed_error"}
        assert not job.done.is_set() and manager.executing
        release.set()
        async with asyncio.timeout(3):
            await job.done.wait()
        assert job.status == status and job.result == {"error": "confirmed_error"}
        assert job.payload == {} and not manager.executing
        assert client.submit.await_count == 1
        assert client.job.await_count == (1 if stage == "job" else 0)
        client.cancel.assert_awaited_once_with(job.request_id)
    finally:
        release.set()
        await manager.close()


async def test_cancel_confirmation_is_not_replaced_by_poll_timeout(hass, monkeypatch):
    accepted = asyncio.Event()
    client = AsyncMock()
    client.submit.return_value = {"status": "accepted"}
    client.cancel.return_value = {"status": "cancelled"}
    monkeypatch.setattr("custom_components.xiaoai_speaker.jobs.JOB_TIMEOUT", 0.01)
    manager = JobManager(hass, client)
    job = await manager.submit("announce", {"text": "synthetic speech"})
    manager.on_change = lambda: accepted.set() if job.status == "accepted" else None
    manager.start()
    try:
        async with asyncio.timeout(3):
            await accepted.wait()
        assert (await manager.cancel(job.request_id))["status"] == "cancelled"
        assert (await manager.wait(job, 3))["status"] == "cancelled"
        assert job.result == {}
        client.job.assert_not_awaited()
    finally:
        await manager.close()


async def test_cancel_after_explicit_busy_does_not_retry_or_contact_device(hass):
    busy = asyncio.Event()
    client = AsyncMock()

    async def submit(*args):
        busy.set()
        raise BridgeError("busy")

    client.submit.side_effect = submit
    manager = JobManager(hass, client)
    job = await manager.submit("announce", {"text": "synthetic speech"})
    manager.start()
    try:
        async with asyncio.timeout(3):
            await busy.wait()
        assert (await manager.cancel(job.request_id))["status"] == "cancelled"
        client.cancel.assert_not_awaited()
        assert client.submit.await_count == 1
    finally:
        await manager.close()


async def test_cancel_during_submission_never_claims_a_local_cancellation(hass):
    entered = asyncio.Event()
    release = asyncio.Event()
    accepted = asyncio.Event()
    client = AsyncMock()

    async def submit(*args):
        entered.set()
        await release.wait()
        return {"status": "accepted"}

    client.submit.side_effect = submit
    client.cancel.side_effect = BridgeError("job_not_found")
    client.job.return_value = {"status": "cancelled"}
    manager = JobManager(hass, client)
    job = await manager.submit("announce", {"text": "synthetic speech"})
    manager.on_change = lambda: accepted.set() if job.status == "accepted" else None
    manager.start()
    try:
        async with asyncio.timeout(3):
            await entered.wait()
        with pytest.raises(BridgeError, match="job_not_found"):
            await manager.cancel(job.request_id)
        assert job.status == "running" and not job.done.is_set()
        release.set()
        async with asyncio.timeout(3):
            await accepted.wait()
        client.cancel.side_effect = None
        client.cancel.return_value = {"status": "cancelled"}
        await manager.cancel(job.request_id)
        assert (await manager.wait(job, 3))["status"] == "cancelled"
        assert client.submit.await_count == 1 and client.cancel.await_count == 2
    finally:
        release.set()
        await manager.close()


async def test_lost_cancel_acknowledgement_is_reconciled_without_resubmission(hass):
    client = AsyncMock()
    client.submit.return_value = {"status": "accepted"}
    client.job.return_value = {"status": "cancelled"}
    client.cancel.side_effect = BridgeError("connection_failed", uncertain=True)
    accepted = asyncio.Event()
    manager = JobManager(hass, client)
    job = await manager.submit("announce", {"text": "synthetic speech"})
    manager.on_change = lambda: accepted.set() if job.status == "accepted" else None
    manager.start()
    try:
        async with asyncio.timeout(3):
            await accepted.wait()
        with pytest.raises(BridgeError, match="connection_failed"):
            await manager.cancel(job.request_id)
        assert not job.done.is_set()
        assert (await manager.wait(job, 3))["status"] == "cancelled"
        assert client.submit.await_count == 1 and client.cancel.await_count == 1
    finally:
        await manager.close()


async def test_bridge_accepted_conversation_cancellation_is_not_faked_locally(hass):
    client = AsyncMock()
    client.submit.return_value = {"status": "accepted"}
    client.job.return_value = {"status": "completed", "reply_text": "done"}
    client.cancel.side_effect = BridgeError("cannot_cancel_running_conversation")
    accepted = asyncio.Event()
    manager = JobManager(hass, client)
    job = await manager.submit("send_text", {"text": "synthetic instruction"})
    manager.on_change = lambda: (
        accepted.set() if client.submit.await_count and job.status == "accepted" else None
    )
    manager.start()
    try:
        async with asyncio.timeout(3):
            await accepted.wait()
        with pytest.raises(BridgeError, match="cannot_cancel_running_conversation"):
            await manager.cancel(job.request_id)
        assert not job.done.is_set() and job.status == "accepted"
        assert (await manager.wait(job, 3))["status"] == "completed"
        assert client.submit.await_count == 1
    finally:
        await manager.close()


async def test_lost_acceptance_is_reconciled_without_reexecuting(hass):
    client = AsyncMock()
    client.submit.side_effect = BridgeError("connection_failed", uncertain=True)
    client.job.return_value = {"status": "completed", "reply_text": "done"}
    manager = JobManager(hass, client)
    job = await manager.submit("send_text", {"text": "open lamp"})
    manager.start()
    result = await manager.wait(job, 3)
    assert result["status"] == "completed"
    assert client.submit.await_count == 1 and client.job.await_count == 1
    assert job.payload == {}
    await manager.close()


async def test_priority_cancel_dedup_and_recursive_guard(hass):
    manager = JobManager(hass, AsyncMock())
    low = await manager.submit("announce", {"text": "low", "request_id": "low"})
    urgent = await manager.submit("announce", {"text": "urgent", "priority": "urgent"})
    assert (await manager.queue.get()).request_id == urgent.request_id
    manager.queue.task_done()
    assert await manager.submit("announce", {"text": "low", "request_id": "low"}) is low
    with pytest.raises(BridgeError, match="request_id_conflict"):
        await manager.submit("announce", {"text": "changed", "request_id": "low"})
    with pytest.raises(BridgeError, match="request_id_conflict"):
        await manager.submit("announce", {"text": "low", "volume": 20, "request_id": "low"})
    await manager.submit("send_text", {"text": "open lamp"})
    with pytest.raises(BridgeError, match="recursive_text_request"):
        await manager.submit("send_text", {"text": "open lamp"})
    assert (await manager.cancel("low"))["status"] == "cancelled"
    await manager.close()
    assert all(j.done.is_set() for j in manager.jobs.values())


async def test_normal_voice_automation_recursion_guard_is_temporary_and_isolated(hass):
    first = JobManager(hass, AsyncMock())
    second = JobManager(hass, AsyncMock())
    first.voice_guard("voice-1", "Open lamp")
    with pytest.raises(BridgeError, match="recursive_text_request"):
        await first.submit("send_text", {"text": " open LAMP ", "use_context": False})
    assert await second.submit("send_text", {"text": "Open lamp"})
    # Announcements may read the same words; they never execute a conversation.
    assert await first.submit("announce", {"text": "Open lamp"})
    first.voice_guard("voice-1", None)
    assert await first.submit("send_text", {"text": "Open lamp"})
    await first.close()
    await second.close()
