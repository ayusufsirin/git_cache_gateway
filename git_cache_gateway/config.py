from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


DEFAULT_CONFIG_PATHS = [
    Path(os.environ.get("GITCACHE_GATEWAY_CONFIG", "")) if os.environ.get("GITCACHE_GATEWAY_CONFIG") else None,
    Path.home() / ".config" / "git-cache-gateway" / "config.toml",
    Path("/etc/git-cache-gateway/config.toml"),
]


@dataclass(frozen=True)
class GitLabConfig:
    base_url: str
    token_env: str = "GITCACHE_GITLAB_TOKEN"
    root_group: str = "mirror"
    visibility: str = "internal"
    # If GitLab rejects the requested visibility during create/update (for
    # example because the parent namespace is private, instance settings
    # disable internal visibility, or the token is not owner/admin), the
    # gateway can still create a usable private mirror and report the policy
    # problem in logs. Set to an empty string and strict_visibility=true to
    # fail hard instead.
    visibility_fallback: str = "private"
    strict_visibility: bool = False
    verify_tls: bool = True
    git_http_username: str = "oauth2"


@dataclass(frozen=True)
class ProviderConfig:
    hosts: list[str] = field(default_factory=lambda: ["github.com", "gitlab.com", "bitbucket.org"])
    default_scheme: str = "https"


@dataclass(frozen=True)
class CacheConfig:
    workdir: Path = Path("/var/cache/git-cache-gateway")
    lockdir: Path = Path("/var/lock/git-cache-gateway")
    update_if_older_than_seconds: int = 3600
    fail_on_update_error: bool = False
    enable_lfs: bool = False


@dataclass(frozen=True)
class UpstreamConfig:
    # Used for cloning/fetching public upstream providers such as GitHub.
    # Keep true when the container has a valid CA store. Set false only for
    # corporate/homelab TLS interception or self-signed CA environments.
    verify_tls: bool = True


@dataclass(frozen=True)
class TLSConfig:
    # Optional custom CA material used by both Python HTTPS calls and git.
    # ca_file is usually a PEM bundle mounted into the container, for example
    # /etc/git-cache-gateway/ca/company-ca.crt. ca_path can point to an
    # OpenSSL-hashed certificate directory. Leave empty to use system CAs.
    ca_file: Path | None = None
    ca_path: Path | None = None




@dataclass(frozen=True)
class GitConfig:
    # Git smart-HTTP transport tuning used by mirror clone/update/push jobs.
    # Large repositories behind corporate proxies/TLS inspection can fail with
    # curl 65 / "unable to rewind rpc post data" unless HTTP/1.1 and a larger
    # post buffer are forced. Set values to empty/0 to let Git defaults apply.
    http_version: str = "HTTP/1.1"
    post_buffer: int = 524288000
    low_speed_limit: int = 0
    low_speed_time: int = 0
    operation_retries: int = 3
    retry_backoff_seconds: float = 5.0
    retry_backoff_multiplier: float = 2.0

@dataclass(frozen=True)
class ServerConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    mode: str = "proxy"  # proxy or redirect
    redirect_include_token: bool = False
    upstream_timeout_seconds: int = 3600
    max_request_body_bytes: int = 2 * 1024 * 1024 * 1024
    # How to handle a repo that is not ready in internal GitLab yet.
    # proxy_upstream: immediately serve GitHub/GitLab.com and mirror in background.
    # wait_for_mirror: old behavior; block the client until local GitLab mirror is ready.
    cache_miss_strategy: str = "proxy_upstream"


@dataclass(frozen=True)
class BackgroundConfig:
    enabled: bool = True
    mirror_workers: int = 4
    max_pending_jobs: int = 256
    refresh_existing: bool = True


@dataclass(frozen=True)
class LoggingConfig:
    # Python logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL.
    level: str = "INFO"
    # Severity used for per-client access logs. Set to DEBUG to hide access
    # logs unless [logging].level is DEBUG.
    access_level: str = "INFO"
    # Log request headers only during debugging; Authorization/Cookie headers are redacted.
    request_headers: bool = False
    # Log upstream GitLab response headers only during debugging; Set-Cookie is redacted.
    upstream_headers: bool = False
    # Log mirror ensure decisions such as create/update/reuse.
    mirror_events: bool = True


@dataclass(frozen=True)
class Config:
    gitlab: GitLabConfig
    providers: ProviderConfig = field(default_factory=ProviderConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    tls: TLSConfig = field(default_factory=TLSConfig)
    git: GitConfig = field(default_factory=GitConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    background: BackgroundConfig = field(default_factory=BackgroundConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def token(self) -> str:
        token = os.environ.get(self.gitlab.token_env, "")
        if not token:
            raise RuntimeError(
                f"Missing GitLab token. Set environment variable {self.gitlab.token_env}."
            )
        return token


def _get(d: dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def load_config(path: str | Path | None = None) -> Config:
    cfg_path: Path | None
    if path is not None:
        cfg_path = Path(path)
    else:
        cfg_path = next((p for p in DEFAULT_CONFIG_PATHS if p and p.exists()), None)

    if cfg_path is None:
        raise FileNotFoundError(
            "No config file found. Set GITCACHE_GATEWAY_CONFIG or create "
            "~/.config/git-cache-gateway/config.toml or /etc/git-cache-gateway/config.toml"
        )

    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    base_url = _get(raw, "gitlab", "base_url")
    if not base_url:
        raise ValueError("[gitlab].base_url is required")

    gitlab = GitLabConfig(
        base_url=str(base_url).rstrip("/"),
        token_env=str(_get(raw, "gitlab", "token_env", default="GITCACHE_GITLAB_TOKEN")),
        root_group=str(_get(raw, "gitlab", "root_group", default="mirror")).strip("/"),
        visibility=str(_get(raw, "gitlab", "visibility", default="internal")),
        visibility_fallback=str(_get(raw, "gitlab", "visibility_fallback", default="private")),
        strict_visibility=bool(_get(raw, "gitlab", "strict_visibility", default=False)),
        verify_tls=bool(_get(raw, "gitlab", "verify_tls", default=True)),
        git_http_username=str(_get(raw, "gitlab", "git_http_username", default="oauth2")),
    )

    hosts = _get(raw, "providers", "hosts", default=["github.com", "gitlab.com", "bitbucket.org"])
    providers = ProviderConfig(
        hosts=[str(h).lower() for h in hosts],
        default_scheme=str(_get(raw, "providers", "default_scheme", default="https")),
    )

    cache = CacheConfig(
        workdir=Path(str(_get(raw, "cache", "workdir", default="/var/cache/git-cache-gateway"))),
        lockdir=Path(str(_get(raw, "cache", "lockdir", default="/var/lock/git-cache-gateway"))),
        update_if_older_than_seconds=int(_get(raw, "cache", "update_if_older_than_seconds", default=3600)),
        fail_on_update_error=bool(_get(raw, "cache", "fail_on_update_error", default=False)),
        enable_lfs=bool(_get(raw, "cache", "enable_lfs", default=False)),
    )

    upstream = UpstreamConfig(
        verify_tls=bool(_get(raw, "upstream", "verify_tls", default=True)),
    )

    ca_file_raw = _get(raw, "tls", "ca_file", default="")
    ca_path_raw = _get(raw, "tls", "ca_path", default="")
    tls = TLSConfig(
        ca_file=Path(str(ca_file_raw)) if str(ca_file_raw).strip() else None,
        ca_path=Path(str(ca_path_raw)) if str(ca_path_raw).strip() else None,
    )

    git_cfg = GitConfig(
        http_version=str(_get(raw, "git", "http_version", default="HTTP/1.1")).strip(),
        post_buffer=int(_get(raw, "git", "post_buffer", default=524288000)),
        low_speed_limit=int(_get(raw, "git", "low_speed_limit", default=0)),
        low_speed_time=int(_get(raw, "git", "low_speed_time", default=0)),
        operation_retries=max(1, int(_get(raw, "git", "operation_retries", default=3))),
        retry_backoff_seconds=max(0.0, float(_get(raw, "git", "retry_backoff_seconds", default=5.0))),
        retry_backoff_multiplier=max(1.0, float(_get(raw, "git", "retry_backoff_multiplier", default=2.0))),
    )

    server = ServerConfig(
        listen_host=str(_get(raw, "server", "listen_host", default="0.0.0.0")),
        listen_port=int(_get(raw, "server", "listen_port", default=8080)),
        mode=str(_get(raw, "server", "mode", default="proxy")).lower(),
        redirect_include_token=bool(_get(raw, "server", "redirect_include_token", default=False)),
        upstream_timeout_seconds=int(_get(raw, "server", "upstream_timeout_seconds", default=3600)),
        max_request_body_bytes=int(_get(raw, "server", "max_request_body_bytes", default=2 * 1024 * 1024 * 1024)),
        cache_miss_strategy=str(_get(raw, "server", "cache_miss_strategy", default="proxy_upstream")).lower(),
    )
    if server.mode not in {"proxy", "redirect"}:
        raise ValueError("[server].mode must be 'proxy' or 'redirect'")
    if server.cache_miss_strategy not in {"proxy_upstream", "wait_for_mirror"}:
        raise ValueError("[server].cache_miss_strategy must be 'proxy_upstream' or 'wait_for_mirror'")

    background = BackgroundConfig(
        enabled=bool(_get(raw, "background", "enabled", default=True)),
        mirror_workers=max(1, int(_get(raw, "background", "mirror_workers", default=4))),
        max_pending_jobs=max(1, int(_get(raw, "background", "max_pending_jobs", default=256))),
        refresh_existing=bool(_get(raw, "background", "refresh_existing", default=True)),
    )

    logging_cfg = LoggingConfig(
        level=str(_get(raw, "logging", "level", default="INFO")).upper(),
        access_level=str(_get(raw, "logging", "access_level", default="INFO")).upper(),
        request_headers=bool(_get(raw, "logging", "request_headers", default=False)),
        upstream_headers=bool(_get(raw, "logging", "upstream_headers", default=False)),
        mirror_events=bool(_get(raw, "logging", "mirror_events", default=True)),
    )

    return Config(
        gitlab=gitlab,
        providers=providers,
        cache=cache,
        upstream=upstream,
        tls=tls,
        git=git_cfg,
        server=server,
        background=background,
        logging=logging_cfg,
    )
