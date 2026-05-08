from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .config import Config
from .mirror import MirrorManager
from .urlmap import RepoMapping

LOG = logging.getLogger("git-cache-gateway.scheduler")


@dataclass(frozen=True)
class MirrorJobSnapshot:
    active: int
    queued_or_running: int
    submitted_total: int
    completed_total: int
    failed_total: int
    rejected_total: int


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
        self._capacity = threading.BoundedSemaphore(cfg.background.max_pending_jobs)
        self._submitted_total = 0
        self._completed_total = 0
        self._failed_total = 0
        self._rejected_total = 0

    def submit(self, mapping: RepoMapping, *, reason: str) -> bool:
        if not self.cfg.background.enabled:
            LOG.debug("mirror_job_skip disabled remote=%s reason=%s", mapping.remote_url, reason)
            return False

        key = mapping.cache_key
        with self._lock:
            existing = self._pending.get(key)
            if existing is not None and not existing.done():
                LOG.debug("mirror_job_dedup remote=%s reason=%s", mapping.remote_url, reason)
                return True

            if not self._capacity.acquire(blocking=False):
                self._rejected_total += 1
                LOG.warning(
                    "mirror_job_rejected queue_full remote=%s reason=%s max_pending=%s",
                    mapping.remote_url,
                    reason,
                    self.cfg.background.max_pending_jobs,
                )
                return False

            self._submitted_total += 1
            future = self._executor.submit(self._run_job, mapping, reason, time.time())
            self._pending[key] = future
            future.add_done_callback(lambda f, cache_key=key: self._done(cache_key, f))
            LOG.info("mirror_job_submitted remote=%s reason=%s", mapping.remote_url, reason)
            return True

    def _run_job(self, mapping: RepoMapping, reason: str, submitted_at: float) -> str:
        started = time.time()
        LOG.info(
            "mirror_job_running remote=%s reason=%s queue_wait_ms=%.1f",
            mapping.remote_url,
            reason,
            (started - submitted_at) * 1000,
        )
        result = self.mirrors.mirror_once(mapping, reason=reason)
        LOG.info(
            "mirror_job_success remote=%s reason=%s elapsed_ms=%.1f",
            mapping.remote_url,
            reason,
            (time.time() - started) * 1000,
        )
        return result

    def _done(self, key: str, future: Future[str]) -> None:
        with self._lock:
            self._pending.pop(key, None)
            self._completed_total += 1
            self._capacity.release()
        try:
            future.result()
        except Exception as e:
            with self._lock:
                self._failed_total += 1
            LOG.exception("mirror_job_failed key=%s error=%r", key, e)

    def snapshot(self) -> MirrorJobSnapshot:
        with self._lock:
            active = sum(1 for f in self._pending.values() if not f.done())
            return MirrorJobSnapshot(
                active=active,
                queued_or_running=len(self._pending),
                submitted_total=self._submitted_total,
                completed_total=self._completed_total,
                failed_total=self._failed_total,
                rejected_total=self._rejected_total,
            )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
