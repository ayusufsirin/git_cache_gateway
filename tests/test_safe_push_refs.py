from pathlib import Path

from git_cache_gateway.config import Config, GitLabConfig
from git_cache_gateway.mirror import MirrorManager
from git_cache_gateway.util import run


def _git(cmd, cwd: Path):
    result = run(["git", *cmd], cwd=cwd, check=False)
    assert result.returncode == 0, result.stderr
    return result


def test_safe_push_refspecs_excludes_hidden_and_head_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "dummy")

    src = tmp_path / "src"
    _git(["init", str(src)], cwd=tmp_path)
    _git(["config", "user.email", "test@example.local"], cwd=src)
    _git(["config", "user.name", "Test User"], cwd=src)
    (src / "README.md").write_text("hello\n")
    _git(["add", "README.md"], cwd=src)
    _git(["commit", "-m", "init"], cwd=src)
    _git(["tag", "v1"], cwd=src)

    mirror = tmp_path / "mirror.git"
    _git(["clone", "--mirror", str(src), str(mirror)], cwd=tmp_path)
    head_sha = run(["git", "rev-parse", "refs/heads/master"], cwd=mirror, check=True).stdout.strip()
    _git(["update-ref", "refs/merge-requests/1/head", head_sha], cwd=mirror)
    _git(["update-ref", "refs/heads/HEAD", head_sha], cwd=mirror)

    cfg = Config(gitlab=GitLabConfig(base_url="https://gitlab.example.local"))
    manager = MirrorManager(cfg)
    refspecs, skipped = manager._safe_push_refspecs(mirror)

    assert "+refs/heads/master:refs/heads/master" in refspecs
    assert "+refs/tags/v1:refs/tags/v1" in refspecs
    assert all("refs/merge-requests" not in spec for spec in refspecs)
    assert all("refs/heads/HEAD" not in spec for spec in refspecs)
    assert "refs/heads/HEAD" in skipped
