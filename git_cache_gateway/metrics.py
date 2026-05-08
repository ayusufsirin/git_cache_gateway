from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, Info, generate_latest

from . import __version__

# Static build/info metric. The value is always 1 with labels carrying metadata.
BUILD_INFO = Info("git_cache_gateway_build", "Git Cache Gateway build information")
BUILD_INFO.info({"version": __version__})

REQUESTS_TOTAL = Counter(
    "git_cache_gateway_requests_total",
    "HTTP requests handled by the gateway.",
    ("method", "target", "status"),
)
REQUEST_DURATION_SECONDS = Histogram(
    "git_cache_gateway_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "target", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, float("inf")),
)
CACHE_EVENTS_TOTAL = Counter(
    "git_cache_gateway_cache_events_total",
    "Cache selection events.",
    ("result",),
)
PROXY_BYTES_TOTAL = Counter(
    "git_cache_gateway_proxy_bytes_total",
    "Bytes proxied by direction and target.",
    ("target", "direction"),
)
PROXY_REQUESTS_TOTAL = Counter(
    "git_cache_gateway_proxy_requests_total",
    "Proxy requests by target and status.",
    ("target", "status"),
)
MIRROR_JOBS_TOTAL = Counter(
    "git_cache_gateway_mirror_jobs_total",
    "Background mirror jobs by result and reason.",
    ("result", "reason"),
)
MIRROR_JOB_DURATION_SECONDS = Histogram(
    "git_cache_gateway_mirror_job_duration_seconds",
    "Background mirror job runtime in seconds.",
    ("result", "reason"),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600, float("inf")),
)
MIRROR_QUEUE_PENDING = Gauge(
    "git_cache_gateway_mirror_queue_pending",
    "Number of mirror jobs currently queued or running.",
)
MIRROR_JOBS_ACTIVE = Gauge(
    "git_cache_gateway_mirror_jobs_active",
    "Number of mirror jobs currently active/running.",
)
MIRROR_JOBS_REJECTED_TOTAL = Counter(
    "git_cache_gateway_mirror_jobs_rejected_total",
    "Mirror jobs rejected because the queue is full.",
    ("reason",),
)
MIRROR_JOBS_DEDUPLICATED_TOTAL = Counter(
    "git_cache_gateway_mirror_jobs_deduplicated_total",
    "Mirror job submissions deduplicated because the repo is already pending/running.",
    ("reason",),
)


@dataclass(frozen=True)
class RecentJobRecord:
    key: str
    remote: str
    reason: str
    result: str
    submitted_at: float
    started_at: float | None
    finished_at: float
    elapsed_seconds: float | None
    error: str | None = None

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {
            "key": self.key,
            "remote": self.remote,
            "reason": self.reason,
            "result": self.result,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "age_seconds": max(0.0, now - self.finished_at),
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
        }


@dataclass
class ServiceStats:
    started_at: float = field(default_factory=time.time)
    _lock: Lock = field(default_factory=Lock)
    request_total: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    proxy_upstream_total: int = 0
    proxy_gitlab_total: int = 0
    proxy_bytes_upstream_to_client: int = 0
    proxy_bytes_client_to_upstream: int = 0
    proxy_bytes_gitlab_to_client: int = 0
    proxy_bytes_client_to_gitlab: int = 0

    def inc_request(self) -> None:
        with self._lock:
            self.request_total += 1

    def inc_cache(self, result: str) -> None:
        with self._lock:
            if result == "hit":
                self.cache_hits += 1
            elif result == "miss":
                self.cache_misses += 1
        CACHE_EVENTS_TOTAL.labels(result=result).inc()

    def inc_proxy(self, target: str) -> None:
        with self._lock:
            if target == "upstream":
                self.proxy_upstream_total += 1
            elif target == "gitlab":
                self.proxy_gitlab_total += 1

    def add_proxy_bytes(self, target: str, direction: str, amount: int) -> None:
        if amount <= 0:
            return
        with self._lock:
            if target == "upstream" and direction == "upstream_to_client":
                self.proxy_bytes_upstream_to_client += amount
            elif target == "upstream" and direction == "client_to_upstream":
                self.proxy_bytes_client_to_upstream += amount
            elif target == "gitlab" and direction == "upstream_to_client":
                self.proxy_bytes_gitlab_to_client += amount
            elif target == "gitlab" and direction == "client_to_upstream":
                self.proxy_bytes_client_to_gitlab += amount
        PROXY_BYTES_TOTAL.labels(target=target, direction=direction).inc(amount)

    def observe_request(self, *, method: str, target: str, status: int, elapsed_seconds: float) -> None:
        status_s = str(status)
        REQUESTS_TOTAL.labels(method=method, target=target, status=status_s).inc()
        REQUEST_DURATION_SECONDS.labels(method=method, target=target, status=status_s).observe(elapsed_seconds)

    def observe_proxy_request(self, *, target: str, status: int) -> None:
        PROXY_REQUESTS_TOTAL.labels(target=target, status=str(status)).inc()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            return {
                "started_at": self.started_at,
                "uptime_seconds": max(0.0, now - self.started_at),
                "requests_total": self.request_total,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "proxy": {
                    "upstream_total": self.proxy_upstream_total,
                    "gitlab_total": self.proxy_gitlab_total,
                    "bytes": {
                        "upstream_to_client": self.proxy_bytes_upstream_to_client,
                        "client_to_upstream": self.proxy_bytes_client_to_upstream,
                        "gitlab_to_client": self.proxy_bytes_gitlab_to_client,
                        "client_to_gitlab": self.proxy_bytes_client_to_gitlab,
                    },
                },
            }


def prometheus_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
