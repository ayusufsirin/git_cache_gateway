from pathlib import Path

from git_cache_gateway.config import load_config


def test_default_visibility_internal(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[gitlab]
base_url = "https://gitlab.example.local"
""")
    cfg = load_config(cfg_path)
    assert cfg.gitlab.visibility == "internal"
    assert "gitlab.com" in cfg.providers.hosts
