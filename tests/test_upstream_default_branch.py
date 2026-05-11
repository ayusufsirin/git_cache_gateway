from pathlib import Path

from git_cache_gateway.config import CacheConfig, Config, GitLabConfig
from git_cache_gateway.mirror import MirrorManager
from git_cache_gateway.urlmap import RepoMapping


def test_choose_default_branch_uses_upstream_head_without_local_cache(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "dummy")
    cfg = Config(
        gitlab=GitLabConfig(base_url="https://gitlab.example.local"),
        cache=CacheConfig(workdir=tmp_path / "cache", lockdir=tmp_path / "locks"),
    )
    manager = MirrorManager(cfg)
    mapping = RepoMapping(
        provider="github.com",
        repo_path="qemu/qemu",
        remote_url="https://github.com/qemu/qemu.git",
        gitlab_full_path="mirror/github.com/qemu/qemu",
        gitlab_http_url="https://gitlab.example.local/mirror/github.com/qemu/qemu.git",
    )

    monkeypatch.setattr(manager, "_upstream_default_branch", lambda _mapping: "master")

    assert manager._choose_gitlab_default_branch(mapping, None, ["master"]) == "master"


def test_choose_default_branch_can_skip_upstream(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "dummy")
    cfg = Config(
        gitlab=GitLabConfig(base_url="https://gitlab.example.local"),
        cache=CacheConfig(workdir=tmp_path / "cache", lockdir=tmp_path / "locks"),
    )
    manager = MirrorManager(cfg)
    mapping = RepoMapping(
        provider="github.com",
        repo_path="example/repo",
        remote_url="https://github.com/example/repo.git",
        gitlab_full_path="mirror/github.com/example/repo",
        gitlab_http_url="https://gitlab.example.local/mirror/github.com/example/repo.git",
    )

    monkeypatch.setattr(manager, "_upstream_default_branch", lambda _mapping: "develop")

    assert manager._choose_gitlab_default_branch(mapping, None, ["master"], use_upstream=False) == "master"
