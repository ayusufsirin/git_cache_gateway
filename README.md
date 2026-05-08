# git-cache-gateway

## v0.2.2 visibility enforcement behavior

`git-cache-gateway enforce-visibility` is best-effort by default. GitLab can
return `403 Forbidden` when the token is not owner/admin, when instance settings
restrict `internal` visibility, or when a subgroup/project cannot be more visible
than its parent. The command now continues, prints skipped objects, and exits 0
unless `--fail-on-skipped` or `--strict` is used.

```bash
git-cache-gateway enforce-visibility
git-cache-gateway enforce-visibility --fail-on-skipped
git-cache-gateway enforce-visibility --strict
```

HTTP Git cache gateway backed by an internal GitLab mirror registry.

The goal is to let normal Git commands keep working:

```bash
git clone https://github.com/org/repo.git
git clone --recurse-submodules https://github.com/org/repo.git
git submodule update --init --recursive
```

while Git transparently rewrites external URLs to the local gateway with `url.insteadOf`:

```text
https://github.com/org/repo.git
  -> http://git-cache.example.local:8080/github.com/org/repo.git
```

The gateway mirrors each requested repo into internal GitLab:

```text
https://gitlab.example.local/mirror/github.com/org/repo.git
```

and serves Git smart HTTP traffic either from the existing GitLab mirror or directly from the upstream provider while a background mirror job runs.

## v0.2 architecture

v0.2 is designed for multiple clients and CI runners:

```text
Git client / runner
  -> Threaded HTTP gateway
      -> cache hit: proxy internal GitLab immediately
      -> cache miss: proxy upstream immediately + enqueue mirror job
      -> stale cache: proxy internal GitLab immediately + enqueue refresh job
  -> background mirror worker pool
      -> clone/update bare mirror
      -> create/repair GitLab project
      -> push --mirror
      -> fix GitLab default_branch/remote HEAD
```

The client does **not** wait for first-time mirroring when:

```toml
[server]
cache_miss_strategy = "proxy_upstream"
```

This is the default. First-time clones use the internet provider directly through the gateway, and future clones use the internal GitLab mirror. If you want the old blocking behavior for tests, set:

```toml
[server]
cache_miss_strategy = "wait_for_mirror"
```

## Why this design

The gateway does **not** reimplement submodule logic. Git still handles clone, fetch, submodule recursion, relative submodule URLs, and nested submodules. The only thing we do is URL-level caching.

When a submodule has:

```ini
url = https://github.com/vendor/lib.git
```

Git rewrites it to:

```text
http://git-cache.example.local:8080/github.com/vendor/lib.git
```

The gateway sees `/github.com/vendor/lib.git`, starts/uses a mirror job for `mirror/github.com/vendor/lib`, and the submodule clone continues normally.

## Modes

### proxy mode, recommended

```toml
[server]
mode = "proxy"
```

The client talks only to the gateway. The gateway injects GitLab credentials when proxying to the internal GitLab. This is the best mode if your internal mirrors are private.

### redirect mode

```toml
[server]
mode = "redirect"
```

The gateway redirects clients to GitLab or upstream. This is simpler, but clients need any required credentials directly. Avoid `redirect_include_token = true` unless you fully trust every client and log sink.

## Config

```toml
[gitlab]
base_url = "https://gitlab.example.local"
token_env = "GITCACHE_GITLAB_TOKEN"
root_group = "mirror"
visibility = "internal"
verify_tls = false
git_http_username = "oauth2"

[providers]
hosts = ["github.com", "gitlab.com", "bitbucket.org"]
default_scheme = "https"

[upstream]
verify_tls = false

[cache]
workdir = "/var/cache/git-cache-gateway"
lockdir = "/var/lock/git-cache-gateway"
update_if_older_than_seconds = 3600
fail_on_update_error = false
enable_lfs = false

[server]
listen_host = "0.0.0.0"
listen_port = 8080
mode = "proxy"
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
level = "INFO"
access_level = "INFO"
request_headers = false
upstream_headers = false
mirror_events = true
```

## Run with Docker

```bash
export GITCACHE_GITLAB_TOKEN="YOUR_GITLAB_TOKEN"
docker compose up -d --build
docker logs -f git-cache-gateway
```

Health/status:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/statusz
```

`/statusz` shows active mirror jobs, submitted/completed/failed job counts, and queue saturation information.

## Configure a client or GitLab runner

```bash
scripts/install-client.sh http://git-cache.example.local:8080/
```

This configures rewrite rules for `github.com`, `gitlab.com`, and `bitbucket.org`:

```bash
git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "https://github.com/"
git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "git@github.com:"
git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "ssh://git@github.com/"
```

## Mirror visibility / permissions

By default, v0.2.1 creates and repairs mirror groups/projects as GitLab `internal` resources:

```toml
[gitlab]
visibility = "internal"
```

That means every GitLab user on your internal instance can see/read the mirror registry, while anonymous users cannot. The gateway applies this visibility through the GitLab API when creating mirror groups and projects, and also repairs existing mirror projects when they are touched by a mirror job.

To repair all existing mirror groups and projects under the configured `root_group`:

```bash
git-cache-gateway enforce-visibility
```

or explicitly:

```bash
git-cache-gateway enforce-visibility --visibility internal
```

The command recursively walks the mirror group tree and updates both groups and projects.

## GitLab.com rewrite

The client installer includes `gitlab.com` by default, along with `github.com` and `bitbucket.org`:

```bash
scripts/install-client.sh http://git-cache.example.local:8080/
```

For only GitLab.com:

```bash
scripts/install-client.sh http://git-cache.example.local:8080/ gitlab.com
```

This adds rules for HTTPS, HTTP, SSH URL, and SCP-like SSH syntax such as `git@gitlab.com:group/repo.git`.

## Test

```bash
GIT_TRACE=1 GIT_CURL_VERBOSE=1 git clone https://github.com/octocat/Hello-World.git
```

Git should request:

```text
http://git-cache.example.local:8080/github.com/octocat/Hello-World.git
```

On cache miss, the clone is served from GitHub immediately while the gateway schedules a background mirror job. After the job completes, future requests are served from internal GitLab.

## CI runner usage

On the runner machine or inside the runner image, apply the same `url.insteadOf` rules before GitLab Runner fetches sources.

For GitLab CI you can use `hooks:pre_get_sources_script`:

```yaml
hooks:
  pre_get_sources_script:
    - git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "https://github.com/"
    - git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "git@github.com:"
    - git config --global --add url."http://git-cache.example.local:8080/gitlab.com/".insteadOf "https://gitlab.com/"
    - git config --global --add url."http://git-cache.example.local:8080/gitlab.com/".insteadOf "git@gitlab.com:"
    - git config --global --add url."http://git-cache.example.local:8080/gitlab.com/".insteadOf "ssh://git@gitlab.com/"

variables:
  GIT_SUBMODULE_STRATEGY: recursive
```

If your runner config does not run hooks early enough for your setup, configure these rules system-wide on the runner host.

## Logging

The gateway prints structured request/access logs and mirror lifecycle events. Configure severity in `config.toml`:

```toml
[logging]
level = "INFO"
access_level = "INFO"
request_headers = false
upstream_headers = false
mirror_events = true
```

Normal logs show:

```text
event=request_start client=192.168.2.10 id=12 method=GET path=/github.com/octocat/Hello-World.git/info/refs query=service=git-upload-pack
event=request_map client=192.168.2.10 id=12 remote=https://github.com/octocat/Hello-World.git mirror=https://gitlab.example.local/mirror/github.com/octocat/Hello-World.git suffix=/info/refs
event=cache_miss client=192.168.2.10 id=12 remote=https://github.com/octocat/Hello-World.git mirror=https://gitlab.example.local/mirror/github.com/octocat/Hello-World.git
mirror_job_submitted remote=https://github.com/octocat/Hello-World.git reason=cache-miss
event=upstream_proxy client=example.local id=12 target=upstream method=GET upstream=https://github.com/octocat/Hello-World.git/info/refs?service=git-upload-pack
event=request_end client=example.local id=12 status=200 target=upstream elapsed_ms=41.3
```

## Important behavior

- The HTTP server is multi-threaded via `ThreadingHTTPServer`.
- First-time cache misses do not block clients when `cache_miss_strategy="proxy_upstream"`.
- A bounded background worker pool mirrors repositories concurrently.
- Duplicate mirror jobs for the same repo are deduplicated in memory.
- Existing GitLab mirrors continue working when internet is down.
- Stale mirror refresh happens in the background if `refresh_existing=true`.
- If the pending queue is full, the client still gets served, but the mirror job is rejected and logged.
- Git LFS support is optional and should be tested separately.

## Current limitations

- This is still a homelab-oriented implementation, not a hardened public proxy.
- Private upstream repositories need credential handling, which is not implemented yet.
- The background queue is in-memory; jobs are not persisted across container restarts.
- Redirect mode does not hide internal GitLab from clients.
- It does not delete mirrors.
