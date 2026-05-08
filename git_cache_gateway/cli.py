from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from .config import load_config
from .mirror import MirrorManager
from .server import run_server
from .urlmap import map_gateway_path, map_remote_url


EXAMPLE_CONFIG = '''# git-cache-gateway config

[gitlab]
base_url = "https://gitlab.example.local"
token_env = "GITCACHE_GITLAB_TOKEN"
root_group = "mirror"
visibility = "internal"
# If GitLab rejects the requested visibility because of namespace policy, token
# permissions, or instance settings, use this fallback so mirrors still work.
# Set visibility_fallback = "" and strict_visibility = true to fail hard instead.
visibility_fallback = "private"
strict_visibility = false
verify_tls = true
git_http_username = "oauth2"

[providers]
hosts = ["github.com", "gitlab.com", "bitbucket.org"]
default_scheme = "https"

[upstream]
# Prefer true. Set false only for quick diagnostics. In company networks, mount
# your company CA and set [tls].ca_file instead of disabling verification.
verify_tls = true

[tls]
# Optional PEM CA bundle used by Python HTTPS calls and git commands.
# Example Docker mount: ./ca/company-ca.crt:/etc/git-cache-gateway/ca/company-ca.crt:ro
ca_file = ""
# Optional OpenSSL-hashed CA directory. Usually leave empty.
ca_path = ""

[cache]
workdir = "/var/cache/git-cache-gateway"
lockdir = "/var/lock/git-cache-gateway"
# 0 disables automatic refresh. Existing mirrors are still served offline.
update_if_older_than_seconds = 3600
# false means: if update fails but GitLab mirror exists, continue using local mirror.
fail_on_update_error = false
# enable only after git-lfs is installed and tested.
enable_lfs = false

[server]
listen_host = "0.0.0.0"
listen_port = 8080
# proxy is better for private GitLab because the gateway injects GitLab auth.
# redirect is simpler but clients need direct GitLab credentials.
mode = "proxy"
# proxy_upstream: first-time clients are served from the original provider while
# the GitLab mirror is created in the background. wait_for_mirror restores the
# old blocking behavior.
cache_miss_strategy = "proxy_upstream"
redirect_include_token = false
upstream_timeout_seconds = 3600
max_request_body_bytes = 2147483648

[background]
enabled = true
mirror_workers = 4
max_pending_jobs = 256
refresh_existing = true

[logging]
# Global gateway log level: DEBUG, INFO, WARNING, ERROR, CRITICAL.
level = "INFO"
# Severity for per-client request/access logs. Set to DEBUG to hide them unless level=DEBUG.
access_level = "INFO"
# Only enable headers during debugging; sensitive headers are redacted.
request_headers = false
upstream_headers = false
# Log mirror decisions such as create, repair, refresh, reuse.
mirror_events = true
'''


def cmd_init_config(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not args.force:
        print(f"Config already exists: {path}. Use --force to overwrite.", file=sys.stderr)
        return 2
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(path)
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    m = map_remote_url(
        args.url,
        cfg.providers.hosts,
        cfg.gitlab.base_url,
        cfg.gitlab.root_group,
        cfg.providers.default_scheme,
    )
    print(f"remote_url={m.remote_url}")
    print(f"gitlab_full_path={m.gitlab_full_path}")
    print(f"gitlab_http_url={m.gitlab_http_url}")
    print(f"gateway_path=/{m.provider}/{m.repo_path}.git")
    return 0


def cmd_ensure(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    m = map_remote_url(
        args.url,
        cfg.providers.hosts,
        cfg.gitlab.base_url,
        cfg.gitlab.root_group,
        cfg.providers.default_scheme,
    )
    manager = MirrorManager(cfg)
    print(manager.ensure(m))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    manager = MirrorManager(cfg)
    for line in manager.doctor():
        print(line)
    return 0



def cmd_enforce_visibility(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    visibility = args.visibility or cfg.gitlab.visibility
    manager = MirrorManager(cfg)
    counts = manager.gitlab.enforce_visibility_tree(
        cfg.gitlab.root_group,
        visibility,
        strict=args.strict,
    )
    print(f"root_group={cfg.gitlab.root_group}")
    print(f"visibility={visibility}")
    print(f"mode={'strict' if args.strict else 'best-effort'}")
    for key in (
        "groups_seen",
        "groups_updated",
        "groups_failed",
        "projects_seen",
        "projects_updated",
        "projects_failed",
    ):
        print(f"{key}={counts[key]}")
    errors = counts.get("errors", [])
    if errors:
        print("errors:")
        for err in errors:
            print(f"  - {err}")
    if not args.strict and (counts.get("groups_failed") or counts.get("projects_failed")):
        print("note=some visibility updates were skipped by GitLab authorization/settings; use an owner/admin token or adjust GitLab visibility restrictions if all objects must become internal")
    return 1 if (args.fail_on_skipped and (counts.get("groups_failed") or counts.get("projects_failed"))) else 0

def cmd_repair_default_branches(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    manager = MirrorManager(cfg)
    mappings = None
    if args.urls:
        mappings = [
            map_remote_url(
                url,
                cfg.providers.hosts,
                cfg.gitlab.base_url,
                cfg.gitlab.root_group,
                cfg.providers.default_scheme,
            )
            for url in args.urls
        ]
    result = manager.repair_default_branches(mappings)
    for key in ("seen", "repaired", "already_ok", "skipped", "failed"):
        print(f"{key}={result[key]}")
    errors = result.get("errors", [])
    if errors:
        print("errors:")
        for err in errors:
            print(f"  - {err}")
    return 1 if result.get("failed") else 0

def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    run_server(cfg)
    return 0


def cmd_install_client(args: argparse.Namespace) -> int:
    gateway = args.gateway.rstrip("/") + "/"
    hosts = args.hosts
    git = shutil.which("git")
    if not git:
        print("git not found", file=sys.stderr)
        return 1

    import subprocess

    def existing_values(key: str) -> set[str]:
        proc = subprocess.run([git, "config", "--global", "--get-all", key], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if proc.returncode != 0:
            return set()
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    for host in hosts:
        base = gateway + host + "/"
        key = f"url.{base}.insteadOf"
        existing = existing_values(key)
        prefixes = [
            f"https://{host}/",
            f"http://{host}/",
            f"ssh://git@{host}/",
            f"git@{host}:",
        ]
        for prefix in prefixes:
            if prefix in existing:
                print(f"already: {prefix} -> {base}")
                continue
            subprocess.check_call([git, "config", "--global", "--add", key, prefix])
            print(f"rewrite: {prefix} -> {base}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="git-cache-gateway")
    p.add_argument("--config", help="Config file path")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init-config", help="write example config")
    init.add_argument("--path", default="~/.config/git-cache-gateway/config.toml")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init_config)

    m = sub.add_parser("map", help="map a remote URL to the internal mirror path")
    m.add_argument("url")
    m.set_defaults(func=cmd_map)

    e = sub.add_parser("ensure", help="ensure mirror exists for a remote URL")
    e.add_argument("url")
    e.set_defaults(func=cmd_ensure)

    d = sub.add_parser("doctor", help="check config, GitLab API and git tools")
    d.set_defaults(func=cmd_doctor)

    v = sub.add_parser("enforce-visibility", help="force all mirror groups/projects under root_group to a visibility")
    v.add_argument("--visibility", choices=["private", "internal", "public"], help="override [gitlab].visibility")
    v.add_argument("--strict", action="store_true", help="fail on the first GitLab API error instead of continuing best-effort")
    v.add_argument("--fail-on-skipped", action="store_true", help="return non-zero if any object could not be updated")
    v.set_defaults(func=cmd_enforce_visibility)

    r = sub.add_parser("repair-default-branches", help="repair GitLab default_branch/remote HEAD for existing mirrors")
    r.add_argument("urls", nargs="*", help="optional remote URLs to repair; if omitted, scans GitLab mirror projects under root_group")
    r.set_defaults(func=cmd_repair_default_branches)

    s = sub.add_parser("serve", help="run the HTTP cache gateway")
    s.set_defaults(func=cmd_serve)

    c = sub.add_parser("install-client", help="configure global git url.insteadOf rules")
    c.add_argument("--gateway", required=True, help="gateway base URL, e.g. http://git-cache.example.local/")
    c.add_argument("--hosts", nargs="+", default=["github.com", "gitlab.com", "bitbucket.org"])
    c.set_defaults(func=cmd_install_client)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
