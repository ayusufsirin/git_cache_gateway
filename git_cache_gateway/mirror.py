from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path, PurePosixPath
from collections.abc import Iterable

from .config import Config
from .gitlab_api import GitLabAPI
from .locks import file_lock
from .urlmap import RepoMapping
from . import __version__
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

    def iter_local_mirror_mappings(self) -> Iterable[RepoMapping]:
        """Yield mappings for local bare mirrors already present on disk.

        This is used by maintenance commands to repair GitLab metadata for
        mirrors that were created by older gateway versions. The Git data may
        be valid while GitLab's project default_branch still points at a
        non-existent branch, causing:

            warning: remote HEAD refers to nonexistent ref, unable to checkout.

        Walking local mirrors gives us the provider/repo path and lets us
        repair those projects without requiring a fresh client request.
        """
        root = self.cfg.cache.workdir / "mirrors"
        if not root.exists():
            return

        for mirror_dir in sorted(root.rglob("*.git")):
            if not mirror_dir.is_dir() or not self._local_mirror_is_git_repo(mirror_dir):
                continue
            try:
                relative = mirror_dir.relative_to(root)
            except ValueError:
                continue
            parts = relative.parts
            if len(parts) < 3:
                # provider/owner/repo.git is the minimum useful shape.
                continue
            provider = parts[0]
            if provider not in self.cfg.providers.hosts:
                continue
            repo_parts = list(parts[1:])
            repo_parts[-1] = repo_parts[-1][:-4]
            repo_path = str(PurePosixPath(*repo_parts))
            remote_url = f"{self.cfg.providers.default_scheme}://{provider}/{repo_path}.git"
            full_path = str(PurePosixPath(self.cfg.gitlab.root_group) / provider / repo_path)
            yield RepoMapping(
                provider=provider,
                repo_path=repo_path,
                remote_url=remote_url,
                gitlab_full_path=full_path,
                gitlab_http_url=f"{self.cfg.gitlab.base_url.rstrip('/')}/{full_path}.git",
            )

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
        if not self.repair_gitlab_default_branch(mapping):
            self._mirror_log(
                "mirror_default_branch_sync_skipped remote=%s project=%s",
                mapping.remote_url,
                mapping.gitlab_full_path,
            )

    def _gitlab_mirror_has_refs(self, mapping: RepoMapping) -> bool:
        repo_url = inject_basic_auth(
            mapping.gitlab_http_url,
            self.cfg.gitlab.git_http_username,
            self.cfg.token,
        )
        result = run(["git", "ls-remote", "--heads", "--tags", repo_url], check=False, env_extra=self._gitlab_git_env())
        return result.returncode == 0 and bool(result.stdout.strip())

    def _gitlab_mirror_heads(self, mapping: RepoMapping) -> list[str]:
        repo_url = inject_basic_auth(
            mapping.gitlab_http_url,
            self.cfg.gitlab.git_http_username,
            self.cfg.token,
        )
        result = run(["git", "ls-remote", "--heads", repo_url], check=False, env_extra=self._gitlab_git_env())
        if result.returncode != 0:
            raise MirrorError(
                f"Failed to list GitLab mirror heads {mapping.gitlab_http_url}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

        heads: list[str] = []
        prefix = "refs/heads/"
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) != 2 or not parts[1].startswith(prefix):
                continue
            branch = parts[1][len(prefix) :]
            if branch and branch not in heads:
                heads.append(branch)
        return heads

    def _choose_gitlab_default_branch(
        self,
        mapping: RepoMapping,
        project_default: str | None,
        gitlab_heads: list[str],
    ) -> str | None:
        if not gitlab_heads:
            return None

        if project_default in gitlab_heads:
            return project_default

        local_default = self._local_mirror_default_branch(self.local_mirror_dir(mapping))
        if local_default in gitlab_heads:
            return local_default

        for preferred in ("main", "master"):
            if preferred in gitlab_heads:
                return preferred

        return gitlab_heads[0]

    def repair_gitlab_default_branch(self, mapping: RepoMapping, project=None) -> bool:
        """Ensure GitLab advertises a cloneable HEAD for this mirror.

        A mirror can have valid branches/tags while GitLab still advertises a
        default branch that does not exist, especially when an empty project was
        created before the first push or after a previous interrupted mirror run.
        In that state `git clone` transfers objects successfully but ends with:

            warning: remote HEAD refers to nonexistent ref, unable to checkout.

        Repairing the GitLab project `default_branch` is cheap compared to a
        clone and is safe to do on the request path for cache hits.
        """
        if project is None:
            project = self.gitlab.get_project(mapping.gitlab_full_path)
        if project is None:
            return False

        heads = self._gitlab_mirror_heads(mapping)
        desired = self._choose_gitlab_default_branch(mapping, project.default_branch, heads)
        if not desired:
            return False

        if project.default_branch == desired:
            return True

        self._mirror_log(
            "mirror_default_branch_repair remote=%s project=%s old=%s new=%s heads=%s",
            mapping.remote_url,
            mapping.gitlab_full_path,
            project.default_branch,
            desired,
            len(heads),
        )
        self.gitlab.set_default_branch(project.id, desired)
        return True

    def gitlab_mirror_ready(self, mapping: RepoMapping) -> bool:
        """Cheap readiness check used on the HTTP request path.

        This intentionally does not clone/fetch/push. It can still perform a
        lightweight GitLab API request and ls-remote against internal GitLab.
        """
        try:
            project = self.gitlab.get_project(mapping.gitlab_full_path)
            if project is None:
                return False
            if not self._gitlab_mirror_has_refs(mapping):
                return False
            try:
                self.repair_gitlab_default_branch(mapping, project=project)
            except Exception as e:
                # Do not make a populated mirror unusable just because HEAD
                # repair failed. The background mirror worker will retry and
                # the warning is less harmful than forcing every request to
                # upstream while offline.
                self._mirror_log(
                    "mirror_default_branch_repair_failed remote=%s project=%s error=%r",
                    mapping.remote_url,
                    mapping.gitlab_full_path,
                    e,
                )
            return True
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

    def _safe_push_refspecs(self, mirror_dir: Path) -> tuple[list[str], list[str]]:
        """Return GitLab-safe mirror refspecs and skipped refs.

        `git push --mirror` pushes every ref namespace, including provider-
        specific/internal refs such as GitLab.com's `refs/merge-requests/*`.
        A GitLab server rejects those as hidden refs. Some upstream repositories
        also carry problematic branch names such as `refs/heads/HEAD`, which
        GitLab rejects as an invalid branch name.

        For a cache used by normal clones/submodules, branches and tags are the
        important public refs. Keep the push intentionally conservative.
        """
        result = run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"],
            cwd=mirror_dir,
            check=False,
        )
        if result.returncode != 0:
            raise MirrorError(
                f"Failed to list local mirror refs\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

        refspecs: list[str] = []
        skipped: list[str] = []
        for line in result.stdout.splitlines():
            ref = line.strip()
            if not ref:
                continue

            if ref.startswith("refs/heads/"):
                branch = ref[len("refs/heads/") :]
                # Git itself can store this ref, but GitLab rejects it as a
                # branch name. Pushing it also confuses default-branch handling.
                if branch == "HEAD" or branch.endswith("/HEAD"):
                    skipped.append(ref)
                    continue
                check = run(["git", "check-ref-format", ref], cwd=mirror_dir, check=False)
                if check.returncode != 0:
                    skipped.append(ref)
                    continue
                refspecs.append(f"+{ref}:{ref}")
                continue

            if ref.startswith("refs/tags/"):
                check = run(["git", "check-ref-format", ref], cwd=mirror_dir, check=False)
                if check.returncode != 0:
                    skipped.append(ref)
                    continue
                refspecs.append(f"+{ref}:{ref}")
                continue

            skipped.append(ref)

        return refspecs, skipped

    @staticmethod
    def _batches(values: list[str], size: int = 200) -> Iterable[list[str]]:
        for index in range(0, len(values), size):
            yield values[index : index + size]

    def _push_local_mirror_to_gitlab(self, mapping: RepoMapping) -> None:
        mirror_dir = self.local_mirror_dir(mapping)
        push_url = inject_basic_auth(
            mapping.gitlab_http_url,
            self.cfg.gitlab.git_http_username,
            self.cfg.token,
        )
        git_env = self._gitlab_git_env()
        refspecs, skipped = self._safe_push_refspecs(mirror_dir)
        if not refspecs:
            raise MirrorError(f"Local mirror has no GitLab-safe refs to push: {mapping.remote_url}")

        self._mirror_log(
            "mirror_push_start remote=%s project=%s refs=%s skipped_refs=%s mode=safe-heads-tags",
            mapping.remote_url,
            mapping.gitlab_full_path,
            len(refspecs),
            len(skipped),
        )
        for skipped_ref in skipped[:20]:
            self._mirror_log("mirror_push_skip_ref remote=%s ref=%s", mapping.remote_url, skipped_ref)
        if len(skipped) > 20:
            self._mirror_log("mirror_push_skip_ref_more remote=%s count=%s", mapping.remote_url, len(skipped) - 20)

        for batch_no, batch in enumerate(self._batches(refspecs), start=1):
            result = run(["git", "push", push_url, *batch], cwd=mirror_dir, check=False, env_extra=git_env)
            if result.returncode != 0:
                raise MirrorError(
                    f"Failed to push mirror refs to GitLab {mapping.gitlab_http_url} batch={batch_no}\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )

        self._mirror_log("mirror_push_done remote=%s project=%s refs=%s", mapping.remote_url, mapping.gitlab_full_path, len(refspecs))
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
            project = self.gitlab.ensure_empty_project(mapping.gitlab_full_path, self.cfg.gitlab.visibility)
            if project.visibility != self.cfg.gitlab.visibility:
                self._mirror_log(
                    "mirror_visibility_fallback remote=%s project=%s requested=%s actual=%s strict=%s",
                    mapping.remote_url,
                    mapping.gitlab_full_path,
                    self.cfg.gitlab.visibility,
                    project.visibility,
                    self.cfg.gitlab.strict_visibility,
                )
            self._push_local_mirror_to_gitlab(mapping)
            self._touch_stamp(mapping)
            self._mirror_log("mirror_job_done reason=%s remote=%s project=%s", reason, mapping.remote_url, mapping.gitlab_full_path)
            return mapping.gitlab_http_url

    def ensure(self, mapping: RepoMapping) -> str:
        """Blocking ensure for CLI and optional wait_for_mirror server mode."""
        return self.mirror_once(mapping, reason="sync-ensure")

    def repair_default_branches(self, mappings: Iterable[RepoMapping] | None = None) -> dict[str, object]:
        """Repair GitLab default_branch metadata for existing mirrors.

        This fixes already-created mirror projects whose data is present but
        whose GitLab default branch/advertised HEAD is stale or invalid. It is
        intentionally safe: each project is repaired independently and failures
        are returned in the result instead of stopping the whole maintenance run.
        """
        result: dict[str, object] = {
            "seen": 0,
            "repaired": 0,
            "already_ok": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }
        selected = list(mappings) if mappings is not None else list(self.iter_local_mirror_mappings())
        errors: list[str] = []

        for mapping in selected:
            result["seen"] = int(result["seen"]) + 1
            try:
                project = self.gitlab.get_project(mapping.gitlab_full_path)
                if project is None:
                    result["skipped"] = int(result["skipped"]) + 1
                    errors.append(f"skip project_missing {mapping.gitlab_full_path}")
                    continue
                heads = self._gitlab_mirror_heads(mapping)
                desired = self._choose_gitlab_default_branch(mapping, project.default_branch, heads)
                if not desired:
                    result["skipped"] = int(result["skipped"]) + 1
                    errors.append(f"skip no_heads {mapping.gitlab_full_path}")
                    continue
                if project.default_branch == desired:
                    result["already_ok"] = int(result["already_ok"]) + 1
                    continue
                self._mirror_log(
                    "mirror_default_branch_repair remote=%s project=%s old=%s new=%s heads=%s",
                    mapping.remote_url,
                    mapping.gitlab_full_path,
                    project.default_branch,
                    desired,
                    len(heads),
                )
                self.gitlab.set_default_branch(project.id, desired)
                result["repaired"] = int(result["repaired"]) + 1
            except Exception as e:
                result["failed"] = int(result["failed"]) + 1
                errors.append(f"fail {mapping.gitlab_full_path}: {e!r}")

        result["errors"] = errors
        return result

    def doctor(self) -> list[str]:
        lines: list[str] = []
        lines.append(f"Version: {__version__}")
        lines.append(f"GitLab: {self.cfg.gitlab.base_url}")
        lines.append(f"Root group: {self.cfg.gitlab.root_group}")
        lines.append(f"Mirror visibility: {self.cfg.gitlab.visibility}")
        lines.append(f"Mirror visibility fallback: {self.cfg.gitlab.visibility_fallback or '<disabled>'}")
        lines.append(f"Strict visibility: {self.cfg.gitlab.strict_visibility}")
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
