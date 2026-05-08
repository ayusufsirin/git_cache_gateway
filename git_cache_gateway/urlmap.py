from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, urlparse


GIT_SERVICE_SUFFIXES = (
    "/info/refs",
    "/git-upload-pack",
    "/git-receive-pack",
    "/HEAD",
    "/objects/",
    "/refs/",
)


@dataclass(frozen=True)
class RepoMapping:
    provider: str
    repo_path: str  # owner/repo, without .git
    remote_url: str
    gitlab_full_path: str  # mirror/github.com/owner/repo
    gitlab_http_url: str  # https://gitlab/mirror/github.com/owner/repo.git
    request_suffix: str = ""  # /info/refs etc.

    @property
    def gitlab_project_api_path(self) -> str:
        return quote(self.gitlab_full_path, safe="")

    @property
    def cache_key(self) -> str:
        return f"{self.provider}/{self.repo_path}"


def _strip_git_suffix(path: str) -> str:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path.strip("/")


def _split_repo_and_suffix(provider: str, path_after_provider: str) -> tuple[str, str]:
    """Split /owner/repo.git/info/refs into (owner/repo, /info/refs)."""
    p = "/" + path_after_provider.strip("/")
    marker = ".git"
    idx = p.find(marker)
    if idx >= 0:
        repo = p[:idx]
        suffix = p[idx + len(marker):]
        return _strip_git_suffix(repo), suffix or ""

    # Fallback for GitLab-style paths that may omit .git. Look for known Git HTTP suffixes.
    for suffix_marker in GIT_SERVICE_SUFFIXES:
        idx = p.find(suffix_marker)
        if idx >= 0:
            repo = p[:idx]
            suffix = p[idx:]
            return _strip_git_suffix(repo), suffix

    return _strip_git_suffix(p), ""


def map_gateway_path(
    raw_path: str,
    provider_hosts: list[str],
    gitlab_base_url: str,
    root_group: str,
    default_scheme: str = "https",
) -> RepoMapping:
    """Map gateway HTTP path to upstream remote and internal GitLab mirror.

    Expected gateway path examples:
      /github.com/owner/repo.git/info/refs
      /gitlab.com/group/subgroup/repo.git/git-upload-pack
    """
    parts = raw_path.strip("/").split("/")
    if len(parts) < 3:
        raise ValueError("Path must be /<provider>/<owner-or-group>/<repo>.git[/...] ")
    provider = parts[0].lower()
    if provider not in provider_hosts:
        raise ValueError(f"Unsupported provider host: {provider}")

    repo_path, suffix = _split_repo_and_suffix(provider, "/".join(parts[1:]))
    if not repo_path or "/" not in repo_path:
        raise ValueError("Repository path must include at least owner/repo")

    remote_url = f"{default_scheme}://{provider}/{repo_path}.git"
    full_path = str(PurePosixPath(root_group) / provider / repo_path)
    gitlab_http_url = f"{gitlab_base_url.rstrip('/')}/{full_path}.git"
    return RepoMapping(
        provider=provider,
        repo_path=repo_path,
        remote_url=remote_url,
        gitlab_full_path=full_path,
        gitlab_http_url=gitlab_http_url,
        request_suffix=suffix,
    )


def map_remote_url(
    remote_url: str,
    provider_hosts: list[str],
    gitlab_base_url: str,
    root_group: str,
    default_scheme: str = "https",
) -> RepoMapping:
    """Map common remote URL syntaxes to internal GitLab path."""
    url = remote_url.strip()

    # SCP-like SSH: git@github.com:owner/repo.git
    if ":" in url and "@" in url and "://" not in url:
        before, after = url.split(":", 1)
        host = before.split("@", 1)[1].lower()
        if host not in provider_hosts:
            raise ValueError(f"Unsupported provider host: {host}")
        repo_path = _strip_git_suffix(after)
        full_path = str(PurePosixPath(root_group) / host / repo_path)
        return RepoMapping(
            provider=host,
            repo_path=repo_path,
            remote_url=f"{default_scheme}://{host}/{repo_path}.git",
            gitlab_full_path=full_path,
            gitlab_http_url=f"{gitlab_base_url.rstrip('/')}/{full_path}.git",
        )

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in provider_hosts:
        raise ValueError(f"Unsupported provider host: {host}")
    repo_path = _strip_git_suffix(parsed.path)
    if not repo_path or "/" not in repo_path:
        raise ValueError("Repository path must include at least owner/repo")
    full_path = str(PurePosixPath(root_group) / host / repo_path)
    return RepoMapping(
        provider=host,
        repo_path=repo_path,
        remote_url=f"{default_scheme}://{host}/{repo_path}.git",
        gitlab_full_path=full_path,
        gitlab_http_url=f"{gitlab_base_url.rstrip('/')}/{full_path}.git",
    )
