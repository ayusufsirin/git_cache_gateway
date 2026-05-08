from pathlib import Path

from git_cache_gateway.config import load_config
from git_cache_gateway.util import git_tls_env


def test_tls_ca_file_configured(tmp_path):
    ca = tmp_path / "company-ca.crt"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'''
[gitlab]
base_url = "https://gitlab.example.local"
verify_tls = true

[upstream]
verify_tls = true

[tls]
ca_file = "{ca}"
ca_path = ""
''')
    cfg = load_config(cfg_path)
    assert cfg.tls.ca_file == ca
    assert cfg.tls.ca_path is None


def test_git_tls_env_uses_ca_file(tmp_path):
    ca = tmp_path / "company-ca.crt"
    env = git_tls_env(verify_tls=True, ca_file=ca, ca_path=None)
    assert env["GIT_SSL_CAINFO"] == str(ca)
    assert env["CURL_CA_BUNDLE"] == str(ca)
    assert "GIT_SSL_NO_VERIFY" not in env


def test_git_tls_env_can_disable_verification():
    env = git_tls_env(verify_tls=False, ca_file="/unused.pem", ca_path=None)
    assert env == {"GIT_SSL_NO_VERIFY": "true"}
