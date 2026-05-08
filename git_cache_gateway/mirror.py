from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from .config import Config
from .gitlab_api import GitLabAPI
from .locks import file_lock
from .urlmap import RepoMapping
from .util import git_tls_env, inject_basic_auth, run


LOG = logging.getLogger("git-cache-gateway.mirror")


class MirrorError(RuntimeError):
    pass


class MirrorManager:
    """GitLab-backed mirror manager.

    The manager can be used in two modes:
      * synchronous (`ensure`) for CLI/manual tests or old blocking behavior
      * asynchronous (`mirror_once`) from the background scheduler

    The request path should usually call the cheap readiness methods first and
    avoid blocking clients on expensive upstream clone/push operations.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.gitlab = GitLabAPI(cfg.gitlab, cfg.token, cfg.tls)
        self.cfg.cache.workdir.mkdir(parents=True, exist_ok=True)
        self.cfg.cache.lockdir.mkdir(parents=True, exist_ok=True)

    def _mirror_log(self, message: str, *args: object) -> None:
        if self.cfg.logging.mirror_events:
            LOG.info(message, *args)

    def _upstream_git_env(self) -> dict[str, str] | None:
        env = git_tls_env(
            verify_tls=self.cfg.upstream.verify_tls,
            ca_file=self.cfg.tls.ca_file,
            ca_path=self.cfg.tls.ca_path,
        )
        return env or None

    def _gitlab_git_env(self) -> dict[str, str] | None:
        env = git_tls_env(
            verify_tls=self.cfg.gitlab.verify_tls,
            ca_file=self.cfg.tls.ca_file,
            ca_path=self.cfg.tls.ca_path,
        )
        return env or None

    def local_mirror_dir(self, mapping: RepoMapping) -> Path:
        return self.cfg.cache.workdir / "mirrors" / mapping.provider / (mapping.repo_path + ".git")

    def _stamp_path(self, mapping: RepoMapping) -> Path:
        return self.cfg.cache.workdir / "stamps" / mapping.provider / (mapping.repo_path.replace("/", "__") + ".stamp")

    def is_stale(self, mapping: RepoMapping) -> bool:
        if self.cfg.cache.update_if_older_than_seconds <= 0:
            return False
        stamp = self._stamp_path(mapping)
        if not stamp.exists():
            return True
        return (time.time() - stamp.stat().st_mtime) > self.cfg.cache.update_if_older_than_seconds

    def _touch_stamp(self, mapping: RepoMapping) -> None:
        stamp = self._stamp_path(mapping)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()

    def _local_mirror_is_git_repo(self, mirror_dir: Path) -> bool:
        return (mirror_dir / "config").exists() and (mirror_dir / "objects").exists()

    def _local_mirror_has_refs(self, mirror_dir: Path) -> bool:
        if not self._local_mirror_is_git_repo(mirror_dir):
            return False
        result = run(["git", "show-ref", "--heads", "--tags"], cwd=mirror_dir, check=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    def _local_mirror_default_branch(self, mirror_dir: Path) -> str | None:
        """Return the upstream default branch stored in the bare mirror."""
        if not self._local_mirror_is_git_repo(mirror_dir):
            return None

        result = run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=mirror_dir, check=False)
        if result.returncode == 0 and result.stdout.strip():
            branch = result.stdout.strip()
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/") :]
            return branch

        result = run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=mirror_dir, check=False)
        if result.returncode != 0:
            return None
        branches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for preferred in ("main", "master"):
            if preferred in branches:
                return preferred
        return branches[0] if branches else None

    def _sync_gitlab_default_branch(self, mapping: RepoMapping) -> None:
        mirror_dir = self.local_mirror_dir(mapping)
        branch = self._local_mirror_default_branch(mirror_dir)
        if not branch:
            return
        project = self.gitlab.get_project(mapping.gitlab_full_path)
        if project is None:
            raise MirrorError(f"GitLab project disappeared while syncing default branch: {mapping.gitlab_full_path}")
        if project.default_branch == branch:
            return
        self._mirror_log(
            "mirror_default_branch_update remote=%s project=%s old=%s new=%s",
            mapping.remote_url,
            mapping.gitlab_full_path,
            project.default_branch,
            branch,
        )
        self.gitlab.set_default_branch(project.id, branch)

    def _gitlab_mirror_has_refs(self, mapping: RepoMapping) -> bool:
        repo_url = inject_basic_auth(
            mapping.gitlab_http_url,
            self.cfg.gitlab.git_http_username,
            self.cfg.token,
        )
        result = run(["git", "ls-remote", "--heads", "--tags", repo_url], check=False, env_extra=self._gitlab_git_env())
        return result.returncode == 0 and bool(result.stdout.strip())

    def gitlab_mirror_ready(self, mapping: RepoMapping) -> bool:
        """Cheap readiness check used on the HTTP request path.

        This intentionally does not clone/fetch/push. It can still perform a
        lightweight GitLab API request and ls-remote against internal GitLab.
        """
        try:
            project = self.gitlab.get_project(mapping.gitlab_full_path)
            if project is None:
                return False
            return self._gitlab_mirror_has_refs(mapping)
        except Exception as e:
            self._mirror_log("mirror_ready_check_failed remote=%s project=%s error=%r", mapping.remote_url, mapping.gitlab_full_path, e)
            return False

    def _clone_or_update_local_mirror(self, mapping: RepoMapping) -> None:
        mirror_dir = self.local_mirror_dir(mapping)
        mirror_dir.parent.mkdir(parents=True, exist_ok=True)
        if mirror_dir.exists() and not self._local_mirror_is_git_repo(mirror_dir):
            shutil.rmtree(mirror_dir)

        if not mirror_dir.exists():
            self._mirror_log("mirror_clone_start remote=%s local=%s", mapping.remote_url, mirror_dir)
            result = run(["git", "clone", "--mirror", mapping.remote_url, str(mirror_dir)], check=False, env_extra=self._upstream_git_env())
            if result.returncode != 0:
                if mirror_dir.exists() and not self._local_mirror_has_refs(mirror_dir):
                    shutil.rmtree(mirror_dir, ignore_errors=True)
                raise MirrorError(
                    f"Failed to clone upstream mirror {mapping.remote_url}\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            self._mirror_log("mirror_clone_done remote=%s local=%s", mapping.remote_url, mirror_dir)
        else:
            self._mirror_log("mirror_update_start remote=%s local=%s", mapping.remote_url, mirror_dir)
            result = run(["git", "remote", "update", "--prune"], cwd=mirror_dir, check=False, env_extra=self._upstream_git_env())
            if result.returncode != 0:
                raise MirrorError(
                    f"Failed to update local mirror {mapping.remote_url}\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            self._mirror_log("mirror_update_done remote=%s local=%s", mapping.remote_url, mirror_dir)

        if not self._local_mirror_has_refs(mirror_dir):
            raise MirrorError(f"Local mirror has no refs after upstream fetch: {mapping.remote_url}")

    def _push_local_mirror_to_gitlab(self, mapping: RepoMapping) -> None:
        mirror_dir = self.local_mirror_dir(mapping)
        push_url = inject_basic_auth(
            mapping.gitlab_http_url,
            self.cfg.gitlab.git_http_username,
            self.cfg.token,
        )
        git_env = self._gitlab_git_env()
        self._mirror_log("mirror_push_start remote=%s project=%s", mapping.remote_url, mapping.gitlab_full_path)
        result = run(["git", "push", "--mirror", push_url], cwd=mirror_dir, check=False, env_extra=git_env)
        if result.returncode != 0:
            raise MirrorError(
                f"Failed to push mirror to GitLab {mapping.gitlab_http_url}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

        self._mirror_log("mirror_push_done remote=%s project=%s", mapping.remote_url, mapping.gitlab_full_path)
        self._sync_gitlab_default_branch(mapping)

        if self.cfg.cache.enable_lfs:
            lfs_fetch = run(["git", "lfs", "fetch", "--all"], cwd=mirror_dir, check=False, env_extra=self._upstream_git_env())
            if lfs_fetch.returncode == 0:
                lfs_push = run(["git", "lfs", "push", "--all", push_url], cwd=mirror_dir, check=False, env_extra=git_env)
                if lfs_push.returncode != 0:
                    raise MirrorError(
                        f"Git LFS push failed for {mapping.remote_url}\n"
                        f"stdout:\n{lfs_push.stdout}\nstderr:\n{lfs_push.stderr}"
                    )

    def mirror_once(self, mapping: RepoMapping, *, reason: str = "manual") -> str:
        """Clone/update upstream and push into GitLab once.

        This is the method background workers call. It clones/updates the local
        bare mirror before creating or repairing the GitLab project, so a failed
        upstream fetch does not create a permanently empty project.
        """
        with file_lock(self.cfg.cache.lockdir, mapping.cache_key):
            self._mirror_log("mirror_job_start reason=%s remote=%s project=%s", reason, mapping.remote_url, mapping.gitlab_full_path)
            self._clone_or_update_local_mirror(mapping)
            self.gitlab.ensure_empty_project(mapping.gitlab_full_path, self.cfg.gitlab.visibility)
            self._push_local_mirror_to_gitlab(mapping)
            self._touch_stamp(mapping)
            self._mirror_log("mirror_job_done reason=%s remote=%s project=%s", reason, mapping.remote_url, mapping.gitlab_full_path)
            return mapping.gitlab_http_url

    def ensure(self, mapping: RepoMapping) -> str:
        """Blocking ensure for CLI and optional wait_for_mirror server mode."""
        return self.mirror_once(mapping, reason="sync-ensure")

    def doctor(self) -> list[str]:
        lines: list[str] = []
        lines.append(f"GitLab: {self.cfg.gitlab.base_url}")
        lines.append(f"Root group: {self.cfg.gitlab.root_group}")
        lines.append(f"Mirror visibility: {self.cfg.gitlab.visibility}")
        lines.append(f"Workdir: {self.cfg.cache.workdir}")
        lines.append(f"Lockdir: {self.cfg.cache.lockdir}")
        lines.append(f"Mode: {self.cfg.server.mode}")
        lines.append(f"Cache miss strategy: {self.cfg.server.cache_miss_strategy}")
        lines.append(f"Background enabled: {self.cfg.background.enabled}")
        lines.append(f"Background mirror workers: {self.cfg.background.mirror_workers}")
        lines.append(f"Background max pending jobs: {self.cfg.background.max_pending_jobs}")
        lines.append(f"GitLab TLS verify: {self.cfg.gitlab.verify_tls}")
        lines.append(f"Upstream TLS verify: {self.cfg.upstream.verify_tls}")
        lines.append(f"TLS CA file: {self.cfg.tls.ca_file or '<system>'}")
        lines.append(f"TLS CA path: {self.cfg.tls.ca_path or '<system>'}")
        if self.cfg.tls.ca_file:
            lines.append(f"TLS CA file exists: {self.cfg.tls.ca_file.exists()}")
        if self.cfg.tls.ca_path:
            lines.append(f"TLS CA path exists: {self.cfg.tls.ca_path.exists()}")
        for binary in (["git", "--version"], ["git", "lfs", "version"]):
            result = run(binary, check=False)
            label = " ".join(binary[:-1]) if len(binary) > 2 else binary[0]
            if result.returncode == 0:
                lines.append(f"{label}: {result.stdout.strip()}")
            else:
                lines.append(f"{label}: unavailable")
        try:
            group = self.gitlab.ensure_group_path(self.cfg.gitlab.root_group)
            lines.append(f"GitLab token/API: OK, root group id={group.id}")
        except Exception as e:
            lines.append(f"GitLab token/API: ERROR: {e}")
        return lines
