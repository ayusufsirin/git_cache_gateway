from git_cache_gateway.config import CacheConfig, Config, GitLabConfig
from git_cache_gateway.gitlab_api import GitLabGroup, GitLabProject
from git_cache_gateway.mirror import MirrorManager


def test_iter_gitlab_mirror_mappings_without_local_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "dummy")
    cfg = Config(
        gitlab=GitLabConfig(base_url="https://gitlab.example.local"),
        cache=CacheConfig(workdir=tmp_path / "cache", lockdir=tmp_path / "locks"),
    )
    manager = MirrorManager(cfg)

    root = GitLabGroup(id=1, full_path="mirror", visibility="internal")
    github = GitLabGroup(id=2, full_path="mirror/github.com", visibility="internal")
    owner = GitLabGroup(id=3, full_path="mirror/github.com/qemu", visibility="internal")
    project = GitLabProject(
        id=4,
        path_with_namespace="mirror/github.com/qemu/qemu",
        http_url_to_repo="https://gitlab.example.local/mirror/github.com/qemu/qemu.git",
        default_branch="main",
        visibility="internal",
    )

    monkeypatch.setattr(manager.gitlab, "get_group", lambda path: root if path == "mirror" else None)
    monkeypatch.setattr(manager.gitlab, "list_group_projects", lambda group_id: [project] if group_id == 3 else [])
    monkeypatch.setattr(manager.gitlab, "list_group_subgroups", lambda group_id: {1: [github], 2: [owner]}.get(group_id, []))

    mappings = list(manager.iter_gitlab_mirror_mappings())

    assert len(mappings) == 1
    assert mappings[0].remote_url == "https://github.com/qemu/qemu.git"
    assert mappings[0].gitlab_full_path == "mirror/github.com/qemu/qemu"


def test_choose_default_branch_uses_upstream_head_without_local_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "dummy")
    cfg = Config(
        gitlab=GitLabConfig(base_url="https://gitlab.example.local"),
        cache=CacheConfig(workdir=tmp_path / "cache", lockdir=tmp_path / "locks"),
    )
    manager = MirrorManager(cfg)

    from git_cache_gateway.urlmap import RepoMapping

    mapping = RepoMapping(
        provider="github.com",
        repo_path="qemu/qemu",
        remote_url="https://github.com/qemu/qemu.git",
        gitlab_full_path="mirror/github.com/qemu/qemu",
        gitlab_http_url="https://gitlab.example.local/mirror/github.com/qemu/qemu.git",
    )
    monkeypatch.setattr(manager, "_upstream_default_branch", lambda _mapping: "master")

    chosen = manager._choose_gitlab_default_branch(mapping, "main", ["master", "stable-8.2"])

    assert chosen == "master"
