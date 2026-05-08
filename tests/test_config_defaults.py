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


def test_default_git_http_tuning(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[gitlab]
base_url = "https://gitlab.example.local"
""")
    cfg = load_config(cfg_path)
    assert cfg.git.http_version == "HTTP/1.1"
    assert cfg.git.post_buffer == 524288000
    assert cfg.git.low_speed_limit == 0
    assert cfg.git.low_speed_time == 0
    assert cfg.git.operation_retries == 3


def test_custom_git_http_tuning(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[gitlab]
base_url = "https://gitlab.example.local"

[git]
http_version = ""
post_buffer = 1048576
low_speed_limit = 1
low_speed_time = 30
operation_retries = 5
retry_backoff_seconds = 0.5
retry_backoff_multiplier = 1.5
""")
    cfg = load_config(cfg_path)
    assert cfg.git.http_version == ""
    assert cfg.git.post_buffer == 1048576
    assert cfg.git.low_speed_limit == 1
    assert cfg.git.low_speed_time == 30
    assert cfg.git.operation_retries == 5
    assert cfg.git.retry_backoff_seconds == 0.5
    assert cfg.git.retry_backoff_multiplier == 1.5
