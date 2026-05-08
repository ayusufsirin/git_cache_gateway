from git_cache_gateway.urlmap import map_gateway_path, map_remote_url


def test_gateway_path_splits_suffix():
    m = map_gateway_path(
        "/github.com/org/repo.git/info/refs",
        ["github.com"],
        "https://gitlab.local",
        "mirror",
    )
    assert m.provider == "github.com"
    assert m.repo_path == "org/repo"
    assert m.request_suffix == "/info/refs"
    assert m.remote_url == "https://github.com/org/repo.git"
    assert m.gitlab_http_url == "https://gitlab.local/mirror/github.com/org/repo.git"


def test_gateway_path_nested_groups():
    m = map_gateway_path(
        "/gitlab.com/a/b/c/repo.git/git-upload-pack",
        ["gitlab.com"],
        "https://gitlab.local",
        "mirror",
    )
    assert m.repo_path == "a/b/c/repo"
    assert m.request_suffix == "/git-upload-pack"
    assert m.gitlab_full_path == "mirror/gitlab.com/a/b/c/repo"


def test_remote_https():
    m = map_remote_url(
        "https://github.com/org/repo.git",
        ["github.com"],
        "https://gitlab.local",
        "mirror",
    )
    assert m.repo_path == "org/repo"
    assert m.gitlab_full_path == "mirror/github.com/org/repo"


def test_remote_ssh_scp():
    m = map_remote_url(
        "git@github.com:org/repo.git",
        ["github.com"],
        "https://gitlab.local",
        "mirror",
    )
    assert m.remote_url == "https://github.com/org/repo.git"
    assert m.gitlab_http_url == "https://gitlab.local/mirror/github.com/org/repo.git"
