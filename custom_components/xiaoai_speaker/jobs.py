"""Bounded per-speaker priority queues live exclusively inside HA."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
import time
import uuid
from dataclasses import dataclass, field

from .client import BridgeError
from .const import JOB_TIMEOUT, MAX_QUEUE, MAX_TEXT, TERMINAL


@dataclass(order=True)
class Job:
    priority: int
    sequence: int
    request_id: str = field(compare=False)
    kind: str = field(compare=False)
    payload: dict = field(compare=False)
    status: str = field(default="accepted", compare=False)
    result: dict = field(default_factory=dict, compare=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, compare=False)
    digest: str = field(default="", compare=False)
    context_id: str | None = field(default=None, compare=False)
    text_digest: str = field(default="", compare=False)


class JobManager:
    def __init__(self, hass, client, on_change=None) -> None:
        self.hass = hass
        self.client = client
        self.on_change = on_change
        self.queue: asyncio.PriorityQueue[Job] = asyncio.PriorityQueue(MAX_QUEUE)
        self.jobs: dict[str, Job] = {}
        self._counter = itertools.count()
        self._worker: asyncio.Task | None = None
        self._closed = False
        self._active_text: set[str] = set()
        self._voice_text: dict[str, tuple[str, float]] = {}

    @staticmethod
    def text_digest(text: str) -> str:
        return hashlib.sha256(text.strip().casefold().encode()).hexdigest()

    def voice_guard(self, request_id: str, text: str | None) -> None:
        """Short-lived fingerprints cover voice → automation → same-text loops."""
        now = time.monotonic()
        self._voice_text = {k: v for k, v in self._voice_text.items() if v[1] > now}
        if text is None:
            self._voice_text.pop(request_id, None)
        elif len(self._voice_text) < 8 or request_id in self._voice_text:
            self._voice_text[request_id] = (self.text_digest(text), now + JOB_TIMEOUT + 60)
        else:
            raise BridgeError("voice_guard_limit")

    @property
    def executing(self) -> bool:
        return any(not job.done.is_set() and job.status != "accepted" for job in self.jobs.values())

    def start(self) -> None:
        self._worker = self.hass.async_create_background_task(self._run(), "XiaoAI job queue")

    async def submit(self, kind: str, payload: dict, context_id: str | None = None) -> Job:
        if kind not in ("announce", "send_text"):
            raise BridgeError("unsupported_action")
        text = payload.get("text", "")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode()) > MAX_TEXT
            or "\0" in text
        ):
            raise BridgeError("invalid_text")
        if self._closed:
            raise BridgeError("queue_closed")
        request_id = payload.get("request_id") or uuid.uuid4().hex
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            raise BridgeError("invalid_request_id")
        digest = hashlib.sha256(
            (
                kind
                + "\0"
                + json.dumps(
                    {k: v for k, v in payload.items() if k != "request_id"}, sort_keys=True
                )
            ).encode()
        ).hexdigest()
        text_digest = self.text_digest(text)
        if request_id in self.jobs:
            existing = self.jobs[request_id]
            if existing.digest != digest:
                raise BridgeError("request_id_conflict")
            return existing
        # A Conversation-triggered automation cannot re-inject the same active instruction.
        if kind == "send_text":
            self.voice_guard("", None)
            if text_digest in self._active_text or any(
                v[0] == text_digest for v in self._voice_text.values()
            ):
                raise BridgeError("recursive_text_request")
        job = Job(
            0 if payload.get("priority") == "urgent" else 1,
            next(self._counter),
            request_id,
            kind,
            {**payload, "request_id": request_id},
            digest=digest,
            context_id=context_id,
            text_digest=text_digest,
        )
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            raise BridgeError("queue_full") from None
        self.jobs[request_id] = job
        if kind == "send_text":
            self._active_text.add(text_digest)
        self._trim()
        self._changed()
        return job

    def _trim(self) -> None:
        finished = [key for key, job in self.jobs.items() if job.done.is_set()]
        for key in finished[:-16]:
            del self.jobs[key]

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    async def wait(self, job: Job, timeout: float = JOB_TIMEOUT) -> dict:
        try:
            async with asyncio.timeout(timeout):
                await job.done.wait()
        except TimeoutError:
            raise BridgeError("job_wait_timeout", uncertain=True) from None
        if job.status == "failed":
            raise BridgeError(job.result.get("error", "job_failed"))
        return {"request_id": job.request_id, "status": job.status, **job.result}

    async def cancel(self, request_id: str) -> dict:
        job = self.jobs.get(request_id)
        if job is None:
            raise BridgeError("job_not_found")
        if job.done.is_set():
            return {"request_id": request_id, "status": job.status}
        if job.status == "accepted":
            job.status = "cancelled"
            job.done.set()
            self._active_text.discard(job.text_digest)
        else:
            await self.client.cancel(request_id)
        self._changed()
        return {"request_id": request_id, "status": job.status}

    async def _execute(self, job: Job) -> None:
        deadline = time.monotonic() + JOB_TIMEOUT
        accepted = False
        while time.monotonic() < deadline:
            if job.status == "cancelled":
                return
            try:
                if not accepted:
                    state = await self.client.submit(job.kind, job.payload)
                    accepted = True
                else:
                    state = await self.client.job(job.request_id)
                job.status = state.get("status", "running")
                job.result = {
                    k: v
                    for k, v in state.items()
                    if k in ("reply_text", "error", "source", "uncertain")
                }
                self._changed()
                if job.status in TERMINAL:
                    return
            except BridgeError as err:
                if err.code == "busy" and not accepted:
                    await asyncio.sleep(1)
                    continue
                if err.uncertain:
                    # Reconcile by ID, never resubmit a potentially executed instruction.
                    accepted = True
                    job.status = "unknown"
                elif err.code in ("connection_failed", "job_not_found") and accepted:
                    job.status = "unknown"
                else:
                    raise
            await asyncio.sleep(0.5)
        job.status = "unknown" if accepted else "failed"
        job.result = {"error": "job_timeout"}

    async def _run(self) -> None:
        while not self._closed:
            job = await self.queue.get()
            try:
                if not job.done.is_set():
                    job.status = "running"
                    self._changed()
                    await self._execute(job)
            except BridgeError as err:
                job.status = "failed"
                job.result = {"error": err.code}
            except asyncio.CancelledError:
                if not job.done.is_set():
                    job.status = "unknown"
                raise
            finally:
                job.done.set()
                self._active_text.discard(job.text_digest)
                job.payload.clear()  # Discard instruction text after execution.
                self._changed()
                self.queue.task_done()

    async def close(self) -> None:
        self._closed = True
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        for job in self.jobs.values():
            if not job.done.is_set():
                job.status = "cancelled"
                job.done.set()
            job.payload.clear()
        self._active_text.clear()
        self._voice_text.clear()
