from __future__ import annotations

import itertools
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Literal
from urllib.parse import urlsplit, urlunsplit

from .config import Config
from .mirror import MirrorManager
from .scheduler import MirrorScheduler
from .urlmap import RepoMapping, map_gateway_path
from .util import basic_auth_header, inject_basic_auth

LOG = logging.getLogger("git-cache-gateway")
ACCESS_LOG = logging.getLogger("git-cache-gateway.access")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}

SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
_REQUEST_COUNTER = itertools.count(1)
TargetKind = Literal["gitlab", "upstream"]


def _levelno(level_name: str) -> int:
    return int(getattr(logging, level_name.upper(), logging.INFO))


def _redact_header(name: str, value: str) -> str:
    return "<redacted>" if name.lower() in SENSITIVE_HEADERS else value


class GitCacheGatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class GitCacheGatewayHandler(BaseHTTPRequestHandler):
    server_version = "git-cache-gateway/0.2.0"

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    @property
    def mirrors(self) -> MirrorManager:
        return self.server.mirrors  # type: ignore[attr-defined]

    @property
    def scheduler(self) -> MirrorScheduler:
        return self.server.scheduler  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        self._access_log("http_message", message=repr(fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        self._handle_request()

    def do_POST(self) -> None:  # noqa: N802
        self._handle_request()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_request()

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _access_log(self, event: str, **fields: object) -> None:
        level = _levelno(self.cfg.logging.access_level)
        parts = [f"event={event}", f"client={self._client_ip()}"]
        parts.extend(f"{k}={v}" for k, v in fields.items() if v is not None)
        ACCESS_LOG.log(level, " ".join(parts))

    def _debug_headers(self, prefix: str, headers: Iterable[tuple[str, str]]) -> None:
        safe = {k: _redact_header(k, v) for k, v in headers}
        LOG.debug("%s headers=%s", prefix, safe)

    def _handle_request(self) -> None:
        req_id = next(_REQUEST_COUNTER)
        start = time.perf_counter()
        parsed_req = urlsplit(self.path)
        if self.cfg.logging.request_headers:
            self._debug_headers(f"request id={req_id}", self.headers.items())

        self._access_log(
            "request_start",
            id=req_id,
            method=self.command,
            path=parsed_req.path,
            query=parsed_req.query or None,
            content_length=self.headers.get("Content-Length"),
            user_agent=repr(self.headers.get("User-Agent")) if self.headers.get("User-Agent") else None,
        )

        if parsed_req.path in {"/healthz", "/readyz"}:
            self._send_text(200, "ok\n")
            self._access_log("request_end", id=req_id, status=200, elapsed_ms=f"{(time.perf_counter() - start) * 1000:.1f}")
            return

        if parsed_req.path in {"/statusz", "/jobs"}:
            snap = self.scheduler.snapshot()
            payload = {
                "ok": True,
                "background": {
                    "enabled": self.cfg.background.enabled,
                    "mirror_workers": self.cfg.background.mirror_workers,
                    "max_pending_jobs": self.cfg.background.max_pending_jobs,
                    "active": snap.active,
                    "queued_or_running": snap.queued_or_running,
                    "submitted_total": snap.submitted_total,
                    "completed_total": snap.completed_total,
                    "failed_total": snap.failed_total,
                    "rejected_total": snap.rejected_total,
                },
                "server": {
                    "mode": self.cfg.server.mode,
                    "cache_miss_strategy": self.cfg.server.cache_miss_strategy,
                },
            }
            self._send_json(200, payload)
            self._access_log("request_end", id=req_id, status=200, elapsed_ms=f"{(time.perf_counter() - start) * 1000:.1f}")
            return

        try:
            mapping = map_gateway_path(
                parsed_req.path,
                self.cfg.providers.hosts,
                self.cfg.gitlab.base_url,
                self.cfg.gitlab.root_group,
                self.cfg.providers.default_scheme,
            )
        except Exception as e:
            self._send_text(404, f"Unsupported Git cache path: {e}\n")
            self._access_log("request_end", id=req_id, status=404, error=repr(e), elapsed_ms=f"{(time.perf_counter() - start) * 1000:.1f}")
            return

        self._access_log(
            "request_map",
            id=req_id,
            remote=mapping.remote_url,
            mirror=mapping.gitlab_http_url,
            suffix=mapping.request_suffix or "/",
        )

        target_kind = self._select_target(mapping, req_id)
        if target_kind == "gitlab":
            upstream_url = self._build_target_url(mapping.gitlab_http_url, mapping.request_suffix, parsed_req.query, target_kind)
        else:
            upstream_url = self._build_target_url(mapping.remote_url, mapping.request_suffix, parsed_req.query, target_kind)

        if self.cfg.server.mode == "redirect":
            self._access_log("upstream_redirect", id=req_id, target=target_kind, upstream=self._sanitize_url(upstream_url))
            self._redirect(upstream_url)
            self._access_log("request_end", id=req_id, status=307, target=target_kind, elapsed_ms=f"{(time.perf_counter() - start) * 1000:.1f}")
        else:
            status = self._proxy(upstream_url, req_id=req_id, target=target_kind)
            self._access_log("request_end", id=req_id, status=status, target=target_kind, elapsed_ms=f"{(time.perf_counter() - start) * 1000:.1f}")

    def _select_target(self, mapping: RepoMapping, req_id: int) -> TargetKind:
        ready = self.mirrors.gitlab_mirror_ready(mapping)
        if ready:
            if self.cfg.background.enabled and self.cfg.background.refresh_existing and self.mirrors.is_stale(mapping):
                self.scheduler.submit(mapping, reason="stale-refresh")
            self._access_log("cache_hit", id=req_id, remote=mapping.remote_url, mirror=mapping.gitlab_http_url)
            return "gitlab"

        self._access_log("cache_miss", id=req_id, remote=mapping.remote_url, mirror=mapping.gitlab_http_url)
        if self.cfg.server.cache_miss_strategy == "wait_for_mirror":
            try:
                self.mirrors.ensure(mapping)
                self._access_log("cache_miss_blocking_ensure_done", id=req_id, remote=mapping.remote_url)
                return "gitlab"
            except Exception as e:
                LOG.exception("blocking mirror ensure failed for %s", mapping.remote_url)
                # If blocking ensure fails, fall back to upstream rather than failing
                # immediately. This keeps clients useful when GitHub is reachable but
                # GitLab mirror creation failed for a transient reason.
                self._access_log("cache_miss_blocking_ensure_failed_fallback_upstream", id=req_id, error=repr(e))

        self.scheduler.submit(mapping, reason="cache-miss")
        return "upstream"

    def _sanitize_url(self, url: str) -> str:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{host}{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def _build_target_url(self, base_repo_url: str, suffix: str, query: str, target: TargetKind) -> str:
        if target == "gitlab" and self.cfg.server.mode == "redirect" and self.cfg.server.redirect_include_token:
            base_repo_url = inject_basic_auth(base_repo_url, self.cfg.gitlab.git_http_username, self.cfg.token)
        parts = urlsplit(base_repo_url)
        path = parts.path.rstrip("/") + (suffix or "")
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))

    def _redirect(self, upstream_url: str) -> None:
        self.send_response(307)
        self.send_header("Location", upstream_url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _proxy(self, upstream_url: str, *, req_id: int, target: TargetKind) -> int:
        body = None
        if self.command in {"POST", "PUT", "PATCH"}:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            if content_length > self.cfg.server.max_request_body_bytes:
                self._send_text(413, "Request body too large\n")
                return 413
            body = self.rfile.read(content_length) if content_length else b""

        headers = self._upstream_headers(target)
        if target == "gitlab":
            headers["Authorization"] = basic_auth_header(self.cfg.gitlab.git_http_username, self.cfg.token)

        self._access_log("upstream_proxy", id=req_id, target=target, method=self.command, upstream=self._sanitize_url(upstream_url))
        request = urllib.request.Request(upstream_url, data=body, headers=headers, method=self.command)
        try:
            context = self._ssl_context(target)
            with urllib.request.urlopen(request, timeout=self.cfg.server.upstream_timeout_seconds, context=context) as resp:  # nosec - user-controlled homelab endpoint
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in HOP_BY_HOP_HEADERS:
                        self.send_header(k, v)
                self.end_headers()
                if self.cfg.logging.upstream_headers:
                    self._debug_headers(f"upstream response id={req_id}", resp.headers.items())
                if self.command != "HEAD":
                    self._copy_stream(resp)
                return int(resp.status)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in HOP_BY_HOP_HEADERS:
                    self.send_header(k, v)
            self.end_headers()
            if self.cfg.logging.upstream_headers:
                self._debug_headers(f"upstream error id={req_id}", e.headers.items())
            if self.command != "HEAD":
                self._copy_stream(e)
            return int(e.code)
        except Exception as e:
            LOG.exception("proxy failed target=%s upstream=%s", target, self._sanitize_url(upstream_url))
            self._send_text(502, f"Proxy to {target} failed: {e}\n")
            return 502

    def _ssl_context(self, target: TargetKind):
        verify = self.cfg.gitlab.verify_tls if target == "gitlab" else self.cfg.upstream.verify_tls
        return None if verify else ssl._create_unverified_context()

    def _upstream_headers(self, target: TargetKind) -> dict[str, str]:
        headers: dict[str, str] = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP_HEADERS:
                continue
            if lk in {"accept-encoding", "content-length"}:
                continue
            # Do not leak client auth intended for the gateway/internal GitLab to
            # public providers on cache misses.
            if target == "upstream" and lk in {"authorization", "cookie"}:
                continue
            headers[k] = v
        return headers

    def _copy_stream(self, src) -> None:
        while True:
            chunk = src.read(1024 * 256)
            if not chunk:
                break
            self.wfile.write(chunk)

    def _send_text(self, status: int, text: str) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_json(self, status: int, payload: dict) -> None:
        raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)


def setup_logging(cfg: Config) -> None:
    logging.basicConfig(
        level=_levelno(cfg.logging.level),
        format="%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s",
    )
    ACCESS_LOG.setLevel(_levelno(cfg.logging.access_level))


def run_server(cfg: Config) -> None:
    setup_logging(cfg)
    mirrors = MirrorManager(cfg)
    scheduler = MirrorScheduler(cfg, mirrors)
    server = GitCacheGatewayHTTPServer((cfg.server.listen_host, cfg.server.listen_port), GitCacheGatewayHandler)
    server.cfg = cfg  # type: ignore[attr-defined]
    server.mirrors = mirrors  # type: ignore[attr-defined]
    server.scheduler = scheduler  # type: ignore[attr-defined]
    LOG.info(
        "listening on %s:%s mode=%s cache_miss_strategy=%s mirror_workers=%s max_pending_jobs=%s",
        cfg.server.listen_host,
        cfg.server.listen_port,
        cfg.server.mode,
        cfg.server.cache_miss_strategy,
        cfg.background.mirror_workers,
        cfg.background.max_pending_jobs,
    )
    try:
        server.serve_forever()
    finally:
        scheduler.shutdown()
