import os

from git_cache_gateway.config import load_config
from git_cache_gateway.mirror import MirrorManager


def test_git_cmd_includes_http_tuning(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[gitlab]
base_url = "https://gitlab.example.local"

[git]
http_version = "HTTP/1.1"
post_buffer = 524288000
low_speed_limit = 0
low_speed_time = 0
operation_retries = 2
""")
    monkeypatch.setenv("GITCACHE_GITLAB_TOKEN", "token")
    manager = MirrorManager(load_config(cfg_path))
    cmd = manager._git_cmd("clone", "--mirror", "https://example/repo.git", "/tmp/repo.git")
    assert cmd[:1] == ["git"]
    assert "http.version=HTTP/1.1" in cmd
    assert "http.postBuffer=524288000" in cmd
    assert "http.lowSpeedLimit=0" in cmd
    assert "http.lowSpeedTime=0" in cmd
    assert cmd[-4:] == ["clone", "--mirror", "https://example/repo.git", "/tmp/repo.git"]
