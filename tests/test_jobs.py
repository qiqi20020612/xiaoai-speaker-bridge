from unittest.mock import AsyncMock

import pytest

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.jobs import JobManager


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
