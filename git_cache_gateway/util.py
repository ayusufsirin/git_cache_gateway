from __future__ import annotations

import base64
import os
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
