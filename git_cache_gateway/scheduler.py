from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .config import Config
from .metrics import (
    MIRROR_JOB_DURATION_SECONDS,
    MIRROR_JOBS_ACTIVE,
    MIRROR_JOBS_DEDUPLICATED_TOTAL,
    MIRROR_JOBS_REJECTED_TOTAL,
    MIRROR_JOBS_TOTAL,
    MIRROR_QUEUE_PENDING,
    RecentJobRecord,
)
from .mirror import MirrorManager
from .urlmap import RepoMapping

LOG = logging.getLogger("git-cache-gateway.scheduler")


@dataclass(frozen=True)
class ActiveJobRecord:
    key: str
    remote: str
    reason: str
    submitted_at: float
    started_at: float | None = None

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {
            "key": self.key,
            "remote": self.remote,
            "reason": self.reason,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "queue_wait_seconds": None if self.started_at is None else max(0.0, self.started_at - self.submitted_at),
            "running_seconds": None if self.started_at is None else max(0.0, now - self.started_at),
        }


@dataclass(frozen=True)
class MirrorJobSnapshot:
    active: int
    queued_or_running: int
    submitted_total: int
    completed_total: int
    failed_total: int
    rejected_total: int
    deduplicated_total: int
    active_jobs: list[dict[str, Any]]
    recent_completed: list[dict[str, Any]]
    recent_failed: list[dict[str, Any]]


class MirrorScheduler:
    """Deduplicated bounded background mirror scheduler."""

    def __init__(self, cfg: Config, mirrors: MirrorManager):
        self.cfg = cfg
        self.mirrors = mirrors
        self._executor = ThreadPoolExecutor(
            max_workers=cfg.background.mirror_workers,
            thread_name_prefix="mirror-worker",
        )
        self._lock = threading.Lock()
        self._pending: dict[str, Future[str]] = {}
        self._active_jobs: dict[str, ActiveJobRecord] = {}
        self._capacity = threading.BoundedSemaphore(cfg.background.max_pending_jobs)
        self._submitted_total = 0
        self._completed_total = 0
        self._failed_total = 0
        self._rejected_total = 0
        self._deduplicated_total = 0
        self._recent_completed: list[RecentJobRecord] = []
        self._recent_failed: list[RecentJobRecord] = []
        self._recent_limit = 25
        self._update_gauges_locked()

    def submit(self, mapping: RepoMapping, *, reason: str) -> bool:
        if not self.cfg.background.enabled:
            LOG.debug("mirror_job_skip disabled remote=%s reason=%s", mapping.remote_url, reason)
            return False

        key = mapping.cache_key
        submitted_at = time.time()
        with self._lock:
            existing = self._pending.get(key)
            if existing is not None and not existing.done():
                self._deduplicated_total += 1
                MIRROR_JOBS_DEDUPLICATED_TOTAL.labels(reason=reason).inc()
                LOG.debug("mirror_job_dedup remote=%s reason=%s", mapping.remote_url, reason)
                return True

            if not self._capacity.acquire(blocking=False):
                self._rejected_total += 1
                MIRROR_JOBS_REJECTED_TOTAL.labels(reason=reason).inc()
                MIRROR_JOBS_TOTAL.labels(result="rejected", reason=reason).inc()
                self._update_gauges_locked()
                LOG.warning(
                    "mirror_job_rejected queue_full remote=%s reason=%s max_pending=%s",
                    mapping.remote_url,
                    reason,
                    self.cfg.background.max_pending_jobs,
                )
                return False

            self._submitted_total += 1
            self._active_jobs[key] = ActiveJobRecord(
                key=key,
                remote=mapping.remote_url,
                reason=reason,
                submitted_at=submitted_at,
            )
            future = self._executor.submit(self._run_job, mapping, reason, submitted_at)
            self._pending[key] = future
            future.add_done_callback(lambda f, cache_key=key: self._done(cache_key, f))
            self._update_gauges_locked()
            LOG.info("mirror_job_submitted remote=%s reason=%s", mapping.remote_url, reason)
            return True

    def _run_job(self, mapping: RepoMapping, reason: str, submitted_at: float) -> str:
        started = time.time()
        key = mapping.cache_key
        with self._lock:
            self._active_jobs[key] = ActiveJobRecord(
                key=key,
                remote=mapping.remote_url,
                reason=reason,
                submitted_at=submitted_at,
                started_at=started,
            )
            self._update_gauges_locked()
        LOG.info(
            "mirror_job_running remote=%s reason=%s queue_wait_ms=%.1f",
            mapping.remote_url,
            reason,
            (started - submitted_at) * 1000,
        )
        result = self.mirrors.mirror_once(mapping, reason=reason)
        elapsed = time.time() - started
        LOG.info(
            "mirror_job_success remote=%s reason=%s elapsed_ms=%.1f",
            mapping.remote_url,
            reason,
            elapsed * 1000,
        )
        return result

    def _done(self, key: str, future: Future[str]) -> None:
        finished = time.time()
        error: Exception | None = None
        try:
            future.result()
        except Exception as e:  # keep outside lock while formatting/logging
            error = e

        with self._lock:
            job = self._active_jobs.pop(key, None)
            self._pending.pop(key, None)
            self._capacity.release()
            if error is None:
                self._completed_total += 1
                result = "success"
            else:
                self._failed_total += 1
                result = "failed"

            reason = job.reason if job else "unknown"
            started_at = job.started_at if job else None
            elapsed = None if started_at is None else max(0.0, finished - started_at)
            record = RecentJobRecord(
                key=key,
                remote=job.remote if job else key,
                reason=reason,
                result=result,
                submitted_at=job.submitted_at if job else finished,
                started_at=started_at,
                finished_at=finished,
                elapsed_seconds=elapsed,
                error=repr(error) if error is not None else None,
            )
            if error is None:
                self._recent_completed.append(record)
                self._recent_completed = self._recent_completed[-self._recent_limit :]
            else:
                self._recent_failed.append(record)
                self._recent_failed = self._recent_failed[-self._recent_limit :]

            MIRROR_JOBS_TOTAL.labels(result=result, reason=reason).inc()
            if elapsed is not None:
                MIRROR_JOB_DURATION_SECONDS.labels(result=result, reason=reason).observe(elapsed)
            self._update_gauges_locked()

        if error is not None:
            LOG.exception("mirror_job_failed key=%s error=%r", key, error)

    def _update_gauges_locked(self) -> None:
        active = sum(1 for job in self._active_jobs.values() if job.started_at is not None)
        MIRROR_QUEUE_PENDING.set(len(self._pending))
        MIRROR_JOBS_ACTIVE.set(active)

    def snapshot(self) -> MirrorJobSnapshot:
        now = time.time()
        with self._lock:
            active_count = sum(1 for job in self._active_jobs.values() if job.started_at is not None)
            return MirrorJobSnapshot(
                active=active_count,
                queued_or_running=len(self._pending),
                submitted_total=self._submitted_total,
                completed_total=self._completed_total,
                failed_total=self._failed_total,
                rejected_total=self._rejected_total,
                deduplicated_total=self._deduplicated_total,
                active_jobs=[job.to_dict(now) for job in self._active_jobs.values()],
                recent_completed=[job.to_dict(now) for job in reversed(self._recent_completed)],
                recent_failed=[job.to_dict(now) for job in reversed(self._recent_failed)],
            )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
