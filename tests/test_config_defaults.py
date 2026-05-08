from git_cache_gateway.config import load_config


def test_default_visibility_internal(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[gitlab]
base_url = "https://gitlab.example.local"
""")
    cfg = load_config(cfg_path)
    assert cfg.gitlab.visibility == "internal"
    assert cfg.gitlab.visibility_fallback == "private"
    assert cfg.gitlab.strict_visibility is False
    assert "gitlab.com" in cfg.providers.hosts


def test_strict_visibility_config(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[gitlab]
base_url = "https://gitlab.example.local"
visibility = "internal"
visibility_fallback = ""
strict_visibility = true
""")
    cfg = load_config(cfg_path)
    assert cfg.gitlab.visibility == "internal"
    assert cfg.gitlab.visibility_fallback == ""
    assert cfg.gitlab.strict_visibility is True
