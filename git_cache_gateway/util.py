from __future__ import annotations

import base64
import os
import ssl
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        env=env,
    )


def inject_basic_auth(url: str, username: str, token: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return url
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    userinfo = f"{quote(username, safe='')}:{quote(token, safe='')}@"
    netloc = f"{userinfo}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def basic_auth_header(username: str, token: str) -> str:
    raw = f"{username}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _nonempty_path(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_ssl_context(*, verify_tls: bool, ca_file: object | None = None, ca_path: object | None = None) -> ssl.SSLContext | None:
    """Return an SSL context for urllib/openers.

    None means "use Python's default system trust store". When a custom CA file
    or CA directory is configured, create an explicit default context using it.
    When verification is disabled, return an unverified context.
    """
    if not verify_tls:
        return ssl._create_unverified_context()  # nosec - explicit homelab/corporate option

    cafile = _nonempty_path(ca_file)
    capath = _nonempty_path(ca_path)
    if cafile or capath:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=cafile, capath=capath)
        return context
    return None


def git_tls_env(*, verify_tls: bool, ca_file: object | None = None, ca_path: object | None = None) -> dict[str, str]:
    """Environment variables that make git/curl use the same TLS policy.

    Git honors GIT_SSL_CAINFO/GIT_SSL_CAPATH. libcurl-based tools also commonly
    honor SSL_CERT_FILE/SSL_CERT_DIR/CURL_CA_BUNDLE, so we export those too.
    """
    if not verify_tls:
        return {"GIT_SSL_NO_VERIFY": "true"}

    env: dict[str, str] = {}
    cafile = _nonempty_path(ca_file)
    capath = _nonempty_path(ca_path)
    if cafile:
        env["GIT_SSL_CAINFO"] = cafile
        env["SSL_CERT_FILE"] = cafile
        env["CURL_CA_BUNDLE"] = cafile
        env["REQUESTS_CA_BUNDLE"] = cafile
    if capath:
        env["GIT_SSL_CAPATH"] = capath
        env["SSL_CERT_DIR"] = capath
    return env
