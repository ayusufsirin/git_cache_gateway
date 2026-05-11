import time

from git_cache_gateway.config import Config, GitLabConfig
from git_cache_gateway.metrics import RecentJobRecord
from git_cache_gateway.scheduler import MirrorScheduler


class FakeMirrors:
    def __init__(self):
        self.calls = []

    def mirror_once(self, mapping, *, reason="manual"):
        self.calls.append((mapping.remote_url, reason))
        return mapping.gitlab_http_url


def make_cfg():
    return Config(
        gitlab=GitLabConfig(
            base_url="https://gitlab.example.local",
            token_env="GITCACHE_GITLAB_TOKEN",
            root_group="mirror",
        )
    )


def test_retry_failed_resubmits_recent_failed_job(monkeypatch):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "token")
    cfg = make_cfg()
    mirrors = FakeMirrors()
    scheduler = MirrorScheduler(cfg, mirrors)
    now = time.time()
    with scheduler._lock:
        scheduler._recent_failed.append(
            RecentJobRecord(
                key="github.com/openssl/openssl",
                remote="https://github.com/openssl/openssl.git",
                reason="cache-miss",
                result="failed",
                submitted_at=now - 10,
                started_at=now - 9,
                finished_at=now - 8,
                elapsed_seconds=1.0,
                error="MirrorError('boom')",
            )
        )

    result = scheduler.retry_failed(provider="github.com", limit=1)

    assert result["matched"] == 1
    assert result["submitted"] == 1
    assert result["not_submitted"] == 0
    assert result["jobs"][0]["remote"] == "https://github.com/openssl/openssl.git"
    scheduler.shutdown()


def test_failed_jobs_returns_newest_first(monkeypatch):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "token")
    scheduler = MirrorScheduler(make_cfg(), FakeMirrors())
    now = time.time()
    with scheduler._lock:
        scheduler._recent_failed.append(
            RecentJobRecord("a", "https://github.com/a/a.git", "cache-miss", "failed", now, now, now, 0.1, "a")
        )
        scheduler._recent_failed.append(
            RecentJobRecord("b", "https://github.com/b/b.git", "cache-miss", "failed", now, now, now + 1, 0.1, "b")
        )

    jobs = scheduler.failed_jobs(limit=1)

    assert len(jobs) == 1
    assert jobs[0]["key"] == "b"
    scheduler.shutdown()
