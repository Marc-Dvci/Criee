"""Bounded, shared audit jobs with JSON progress for proxies that buffer SSE."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
import time
from typing import Any

from .ledger import Ledger


@dataclass
class AuditJob:
    recent: int | None
    progress: dict[str, Any]
    created: float
    task: asyncio.Task | None = None


class VerificationJobs:
    def __init__(self, ledger: Ledger, capacity: int = 8):
        self.ledger = ledger
        self.capacity = capacity
        self.jobs: dict[str, AuditJob] = {}
        self._lock = asyncio.Lock()

    def get(self, job_id: str) -> dict:
        return {"id": job_id, **self.jobs[job_id].progress}

    async def start(self, recent: int | None = None) -> dict:
        async with self._lock:
            now = time.monotonic()
            for job_id, job in list(self.jobs.items()):
                if job.progress["done"] and now - job.created > 300:
                    del self.jobs[job_id]
            # Judges share a running audit of the same scope, rather than duplicating crypto work.
            for job_id, job in self.jobs.items():
                if job.recent == recent and not job.progress["done"]:
                    return self.get(job_id)
            if len(self.jobs) >= self.capacity:
                finished = next((key for key, job in self.jobs.items() if job.progress["done"]), None)
                if finished is None:
                    raise RuntimeError("Audits are busy. Please try again shortly.")
                del self.jobs[finished]
            iterator = self.ledger.verify_progress(recent=recent)
            first = await asyncio.to_thread(next, iterator)
            job_id = secrets.token_hex(16)
            job = AuditJob(recent, first, now)
            self.jobs[job_id] = job

            async def run():
                try:
                    while True:
                        progress = await asyncio.to_thread(next, iterator, None)
                        if progress is None:
                            raise RuntimeError("audit ended without a result")
                        if progress["done"]:
                            progress["public_key"] = self.ledger.signer.public_pem()
                        job.progress = progress
                        if progress["done"]:
                            return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    job.progress = {**job.progress, "done": True, "ok": False, "reason": "verification unavailable"}

            job.task = asyncio.create_task(run())
            return self.get(job_id)

    async def close(self):
        tasks = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
