from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import GitLabConfig, TLSConfig
from .util import build_ssl_context


@dataclass(frozen=True)
class GitLabProject:
    id: int
    path_with_namespace: str
    http_url_to_repo: str
    default_branch: str | None = None
    visibility: str | None = None


@dataclass(frozen=True)
class GitLabGroup:
    id: int
    full_path: str
    visibility: str | None = None


class GitLabAPI:
    def __init__(self, cfg: GitLabConfig, token: str, tls: TLSConfig | None = None):
        self.cfg = cfg
        self.token = token
        self.api_base = cfg.base_url.rstrip("/") + "/api/v4"
        self.ssl_context = build_ssl_context(
            verify_tls=cfg.verify_tls,
            ca_file=tls.ca_file if tls else None,
            ca_path=tls.ca_path if tls else None,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict | None = None,
        ok: tuple[int, ...] = (200, 201),
    ) -> dict | list | None:
        body = None
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json"}
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(self.api_base + path, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=60, context=self.ssl_context) as resp:  # nosec - user-controlled homelab endpoint
                if resp.status not in ok:
                    raise RuntimeError(f"GitLab API unexpected status {resp.status}: {path}")
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except HTTPError as e:
            if e.code in ok:
                raw = e.read()
                return json.loads(raw.decode("utf-8")) if raw else None
            raise

    def _paged_get(self, path: str, *, per_page: int = 100) -> list[dict]:
        """Read a GitLab list endpoint using simple page iteration.

        We stop when the returned page is shorter than per_page. This avoids
        needing response headers and is enough for the mirror-admin operations.
        """
        out: list[dict] = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            data = self._request("GET", f"{path}{sep}{urlencode({'per_page': per_page, 'page': page})}")
            assert isinstance(data, list)
            out.extend(item for item in data if isinstance(item, dict))
            if len(data) < per_page:
                break
            page += 1
        return out

    @staticmethod
    def _project_from_data(data: dict) -> GitLabProject:
        return GitLabProject(
            id=int(data["id"]),
            path_with_namespace=str(data["path_with_namespace"]),
            http_url_to_repo=str(data["http_url_to_repo"]),
            default_branch=str(data["default_branch"]) if data.get("default_branch") else None,
            visibility=str(data["visibility"]) if data.get("visibility") else None,
        )

    @staticmethod
    def _group_from_data(data: dict) -> GitLabGroup:
        return GitLabGroup(
            id=int(data["id"]),
            full_path=str(data["full_path"]),
            visibility=str(data["visibility"]) if data.get("visibility") else None,
        )

    def get_group(self, full_path: str) -> GitLabGroup | None:
        encoded = quote(full_path.strip("/"), safe="")
        try:
            data = self._request("GET", f"/groups/{encoded}")
        except HTTPError as e:
            if e.code == 404:
                return None
            raise
        assert isinstance(data, dict)
        return self._group_from_data(data)

    @staticmethod
    def _http_error_text(error: HTTPError) -> str:
        try:
            raw = error.read()
        except Exception:
            raw = b""
        if not raw:
            return f"HTTP {error.code}: {error.reason}"
        try:
            text = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            text = repr(raw)
        return f"HTTP {error.code}: {error.reason}: {text}"

    def set_group_visibility(self, group_id: int, visibility: str) -> None:
        self._request("PUT", f"/groups/{group_id}", data={"visibility": visibility}, ok=(200,))

    def try_set_group_visibility(self, group: GitLabGroup, visibility: str) -> str | None:
        try:
            self.set_group_visibility(group.id, visibility)
            return None
        except HTTPError as e:
            return self._http_error_text(e)

    def ensure_group_visibility(self, group: GitLabGroup, visibility: str, *, strict: bool = True) -> GitLabGroup:
        if group.visibility == visibility:
            return group
        if strict:
            self.set_group_visibility(group.id, visibility)
            return GitLabGroup(id=group.id, full_path=group.full_path, visibility=visibility)
        err = self.try_set_group_visibility(group, visibility)
        if err is None:
            return GitLabGroup(id=group.id, full_path=group.full_path, visibility=visibility)
        return group

    def ensure_group_path(self, full_path: str, visibility: str | None = None) -> GitLabGroup:
        """Ensure nested groups exist and return the deepest group.

        Existing and newly-created groups are forced to the requested visibility
        when visibility is provided. This is important for a mirror registry,
        because GitLab may create nested groups with default/private visibility.
        """
        desired_visibility = visibility or self.cfg.visibility
        parts = [p for p in full_path.strip("/").split("/") if p]
        if not parts:
            raise ValueError("Group path cannot be empty")

        current_path = ""
        parent_id: int | None = None
        group: GitLabGroup | None = None
        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else part
            group = self.get_group(current_path)
            if group is not None:
                group = self.ensure_group_visibility(group, desired_visibility)
                parent_id = group.id
                continue
            payload: dict[str, object] = {"name": part, "path": part, "visibility": desired_visibility}
            if parent_id is not None:
                payload["parent_id"] = parent_id
            data = self._request("POST", "/groups", data=payload, ok=(201,))
            assert isinstance(data, dict)
            group = self._group_from_data(data)
            if group.visibility != desired_visibility:
                group = self.ensure_group_visibility(group, desired_visibility)
            parent_id = group.id
        assert group is not None
        return group

    def get_project(self, full_path: str) -> GitLabProject | None:
        encoded = quote(full_path.strip("/"), safe="")
        try:
            data = self._request("GET", f"/projects/{encoded}")
        except HTTPError as e:
            if e.code == 404:
                return None
            raise
        assert isinstance(data, dict)
        return self._project_from_data(data)

    def set_project_visibility(self, project_id: int, visibility: str) -> None:
        self._request("PUT", f"/projects/{project_id}", data={"visibility": visibility}, ok=(200,))

    def try_set_project_visibility(self, project: GitLabProject, visibility: str) -> str | None:
        try:
            self.set_project_visibility(project.id, visibility)
            return None
        except HTTPError as e:
            return self._http_error_text(e)

    def ensure_project_visibility(self, project: GitLabProject, visibility: str, *, strict: bool = True) -> GitLabProject:
        if project.visibility == visibility:
            return project
        if strict:
            self.set_project_visibility(project.id, visibility)
            return GitLabProject(
                id=project.id,
                path_with_namespace=project.path_with_namespace,
                http_url_to_repo=project.http_url_to_repo,
                default_branch=project.default_branch,
                visibility=visibility,
            )
        err = self.try_set_project_visibility(project, visibility)
        if err is None:
            return GitLabProject(
                id=project.id,
                path_with_namespace=project.path_with_namespace,
                http_url_to_repo=project.http_url_to_repo,
                default_branch=project.default_branch,
                visibility=visibility,
            )
        return project

    def ensure_empty_project(self, full_path: str, visibility: str) -> GitLabProject:
        project = self.get_project(full_path)
        if project is not None:
            return self.ensure_project_visibility(project, visibility)

        parts = [p for p in full_path.strip("/").split("/") if p]
        if len(parts) < 2:
            raise ValueError("Project full path must include group/project")
        project_path = parts[-1]
        group_path = "/".join(parts[:-1])
        namespace = self.ensure_group_path(group_path, visibility=visibility)
        payload = {
            "name": project_path,
            "path": project_path,
            "namespace_id": namespace.id,
            "visibility": visibility,
            "initialize_with_readme": False,
        }
        data = self._request("POST", "/projects", data=payload, ok=(201,))
        assert isinstance(data, dict)
        project = self._project_from_data(data)
        return self.ensure_project_visibility(project, visibility)

    def set_default_branch(self, project_id: int, branch: str) -> None:
        # GitLab uses project.default_branch to advertise remote HEAD over smart HTTP.
        # A mirror push creates refs, but it does not always update this metadata,
        # especially when the project was first created empty.
        self._request("PUT", f"/projects/{project_id}", data={"default_branch": branch}, ok=(200,))

    def list_project_branches(self, project_id: int) -> list[str]:
        data = self._request("GET", f"/projects/{project_id}/repository/branches?per_page=100")
        assert isinstance(data, list)
        return [str(item["name"]) for item in data if isinstance(item, dict) and item.get("name")]

    def list_group_subgroups(self, group_id: int) -> list[GitLabGroup]:
        items = self._paged_get(f"/groups/{group_id}/subgroups")
        return [self._group_from_data(item) for item in items]

    def list_group_projects(self, group_id: int) -> list[GitLabProject]:
        items = self._paged_get(f"/groups/{group_id}/projects?include_subgroups=false")
        return [self._project_from_data(item) for item in items]

    def enforce_visibility_tree(
        self,
        root_group_path: str,
        visibility: str,
        *,
        strict: bool = False,
    ) -> dict[str, object]:
        """Force every subgroup and project under root_group_path to visibility.

        By default this is best-effort: GitLab may return 403 when the token is
        not owner/admin, when instance settings restrict visibility levels, or
        when a subgroup/project is not allowed to become more visible than its
        parent. We keep walking and return failed object details instead of
        crashing. Pass strict=True to fail on the first GitLab API error.
        """
        # Do not call ensure_group_path here: an existing root group may be
        # private and not mutable by this token. Fetch it first so we can report
        # the exact 403 as a skipped update rather than crashing before traversal.
        root = self.get_group(root_group_path)
        if root is None:
            # Root does not exist; this is a real setup action, so keep strict
            # behavior. New groups/projects will still be created with visibility.
            root = self.ensure_group_path(root_group_path, visibility=visibility)

        counts: dict[str, object] = {
            "groups_seen": 0,
            "groups_updated": 0,
            "groups_failed": 0,
            "projects_seen": 0,
            "projects_updated": 0,
            "projects_failed": 0,
            "errors": [],
        }

        def add_error(kind: str, full_path: str, err: str) -> None:
            errors = counts["errors"]
            assert isinstance(errors, list)
            errors.append(f"{kind}:{full_path}: {err}")

        def walk(group: GitLabGroup) -> None:
            counts["groups_seen"] = int(counts["groups_seen"]) + 1
            current_group = group
            if group.visibility != visibility:
                try:
                    self.set_group_visibility(group.id, visibility)
                    counts["groups_updated"] = int(counts["groups_updated"]) + 1
                    current_group = GitLabGroup(id=group.id, full_path=group.full_path, visibility=visibility)
                except HTTPError as e:
                    if strict:
                        raise
                    counts["groups_failed"] = int(counts["groups_failed"]) + 1
                    add_error("group", group.full_path, self._http_error_text(e))

            for project in self.list_group_projects(current_group.id):
                counts["projects_seen"] = int(counts["projects_seen"]) + 1
                if project.visibility != visibility:
                    try:
                        self.set_project_visibility(project.id, visibility)
                        counts["projects_updated"] = int(counts["projects_updated"]) + 1
                    except HTTPError as e:
                        if strict:
                            raise
                        counts["projects_failed"] = int(counts["projects_failed"]) + 1
                        add_error("project", project.path_with_namespace, self._http_error_text(e))

            for subgroup in self.list_group_subgroups(current_group.id):
                walk(subgroup)

        walk(root)
        return counts

    def list_project_variables(self, project_id: int) -> list[dict]:
        data = self._request("GET", f"/projects/{project_id}/variables")
        assert isinstance(data, list)
        return data

    def project_url(self, full_path: str) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/{full_path.strip('/')}.git"
