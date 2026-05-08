from pathlib import Path


def test_dockerfile_installs_ca_before_pip():
    text = Path("Dockerfile").read_text()
    assert "install-ca-certificates.sh /app/ca" in text
    assert text.index("install-ca-certificates.sh /app/ca") < text.index("pip install")


def test_entrypoint_uses_same_ca_installer():
    text = Path("scripts/docker-entrypoint.sh").read_text()
    assert "install-ca-certificates.sh" in text
    assert "GITCACHE_EXTRA_CA_DIR" in text
