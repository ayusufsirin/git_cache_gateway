from git_cache_gateway.config import Config, GitLabConfig
from git_cache_gateway.mirror import MirrorManager
from git_cache_gateway.urlmap import RepoMapping


def _mapping() -> RepoMapping:
    return RepoMapping(
        provider="github.com",
        repo_path="qemu/qemu",
        remote_url="https://github.com/qemu/qemu.git",
        gitlab_full_path="mirror/github.com/qemu/qemu",
        gitlab_http_url="https://gitlab.example.local/mirror/github.com/qemu/qemu.git",
    )


def test_choose_default_branch_repairs_nonexistent_gitlab_default(monkeypatch):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "dummy")
    manager = MirrorManager(Config(gitlab=GitLabConfig(base_url="https://gitlab.example.local")))
    monkeypatch.setattr(manager, "_local_mirror_default_branch", lambda _mirror_dir: "master")

    chosen = manager._choose_gitlab_default_branch(_mapping(), "main", ["master", "stable-8.2"])

    assert chosen == "master"


def test_choose_default_branch_keeps_valid_gitlab_default(monkeypatch):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "dummy")
    manager = MirrorManager(Config(gitlab=GitLabConfig(base_url="https://gitlab.example.local")))
    monkeypatch.setattr(manager, "_local_mirror_default_branch", lambda _mirror_dir: "master")

    chosen = manager._choose_gitlab_default_branch(_mapping(), "stable-8.2", ["master", "stable-8.2"])

    assert chosen == "stable-8.2"
