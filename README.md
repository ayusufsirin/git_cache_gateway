# Git Cache Gateway

**Git Cache Gateway** is an HTTP Git caching gateway backed by an internal GitLab instance. It is designed for homelab, company LAN, and CI environments where internet access may be slow, unstable, or unavailable for long periods.

The gateway allows normal Git commands to keep working:

```bash
git clone https://github.com/org/repo.git
git clone --recurse-submodules https://github.com/org/repo.git
git submodule update --init --recursive
```

External Git URLs are transparently rewritten to the local gateway with Git’s native `url.insteadOf` mechanism:

```text
https://github.com/org/repo.git
  -> http://git-cache.example.local:8080/github.com/org/repo.git
```

The gateway then mirrors the requested repository into an internal GitLab mirror registry:

```text
https://gitlab.example.local/mirror/github.com/org/repo.git
```

On future requests, Git traffic is served from the internal GitLab mirror instead of the public internet.

---

## Key Features

* Transparent Git caching using `url.insteadOf`
* Works with normal `git clone`, `git fetch`, and recursive submodules
* Does not reimplement Git submodule logic
* Mirrors repositories into an internal GitLab instance
* Supports GitHub, GitLab.com, Bitbucket, and configurable providers
* Multi-client HTTP server using `ThreadingHTTPServer`
* Non-blocking cache-miss behavior
* Background mirror worker pool
* Stale mirror refresh in the background
* Configurable mirror visibility, such as GitLab `internal`
* Best-effort visibility enforcement for existing mirrors
* Docker-friendly deployment
* Company/internal CA certificate support
* Build-time CA support for `pip install`
* Structured request and mirror lifecycle logging
* Health and status endpoints

---

## Architecture

Git Cache Gateway is designed around a simple principle:

> Git should still do Git’s job. The gateway should only handle URL-level caching and mirroring.

The gateway does not manually clone submodules or rewrite `.gitmodules`. Instead, Git’s own submodule logic remains responsible for:

* recursive submodules
* nested submodules
* relative submodule URLs
* fetch and checkout behavior
* standard Git smart HTTP protocol behavior

The gateway only ensures that every external Git URL is routed through the cache layer.

```text
Git client / GitLab Runner
  -> Git url.insteadOf rewrite
  -> Git Cache Gateway
      -> cache hit:
           proxy internal GitLab mirror immediately
      -> cache miss:
           proxy upstream provider immediately
           enqueue background mirror job
      -> stale cache:
           proxy internal GitLab mirror immediately
           enqueue background refresh job
  -> background mirror worker pool
      -> clone/update bare mirror
      -> create/repair GitLab group/project
      -> push --mirror
      -> repair GitLab default branch / remote HEAD
```

---

## Cache-Miss Behavior

By default, first-time requests do not wait for the mirror operation to finish.

```toml
[server]
cache_miss_strategy = "proxy_upstream"
```

With this mode:

1. The client requests a repository through the gateway.
2. If no internal mirror exists yet, the gateway proxies the request directly to the upstream provider, such as GitHub.
3. In the background, the gateway creates or updates the internal GitLab mirror.
4. Future requests are served from the internal GitLab mirror.

This is the recommended mode for CI runners and multiple users.

For testing or strict offline-preparation workflows, the old blocking behavior can be enabled:

```toml
[server]
cache_miss_strategy = "wait_for_mirror"
```

In that mode, the client waits until the mirror is created before Git traffic is served.

---

## Why `url.insteadOf` Is Used

Git Cache Gateway relies on Git’s native URL rewrite system.

For example, this command:

```bash
git clone --recurse-submodules https://github.com/org/repo.git
```

is internally rewritten by Git to:

```text
http://git-cache.example.local:8080/github.com/org/repo.git
```

If the repository contains a submodule like this:

```ini
[submodule "vendor/lib"]
  path = vendor/lib
  url = https://github.com/vendor/lib.git
```

Git also rewrites the submodule URL to:

```text
http://git-cache.example.local:8080/github.com/vendor/lib.git
```

Therefore, submodules are handled naturally by Git, while the gateway sees each external repository request and mirrors it independently.

---

## Operating Modes

### Proxy Mode

Recommended mode:

```toml
[server]
mode = "proxy"
```

In proxy mode, clients talk only to the gateway. The gateway proxies Git smart HTTP traffic to either:

* the internal GitLab mirror, or
* the upstream provider on cache miss.

This mode is best when internal GitLab mirrors are private or internal, because the gateway can inject GitLab credentials internally without exposing them to clients.

### Redirect Mode

```toml
[server]
mode = "redirect"
```

In redirect mode, the gateway redirects clients to GitLab or the upstream provider.

This is simpler, but clients must have direct access and credentials for the redirected target.

Avoid this unless you specifically need it:

```toml
redirect_include_token = true
```

Including tokens in redirects can expose credentials through browser history, Git logs, proxy logs, or CI logs.

---

## Configuration

Example `config.toml`:

```toml
[gitlab]
base_url = "https://gitlab.example.local"
token_env = "GITCACHE_GITLAB_TOKEN"
root_group = "mirror"
visibility = "internal"
# Fallback used when GitLab rejects the requested visibility during group/project creation.
# Set to "" with strict_visibility = true when visibility policy failures should fail mirror jobs.
visibility_fallback = "private"
strict_visibility = false
verify_tls = true
git_http_username = "oauth2"

[providers]
hosts = ["github.com", "gitlab.com", "bitbucket.org"]
default_scheme = "https"

[upstream]
verify_tls = true

[tls]
# Usually empty when CA certificates are installed into the container OS trust store.
ca_file = ""
ca_path = ""

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

---

## GitLab Token

The gateway requires a GitLab token to create groups, create projects, push mirrors, and update project visibility.

Recommended token permissions:

```text
api
read_repository
write_repository
```

Export it before starting the service:

```bash
export GITCACHE_GITLAB_TOKEN="YOUR_GITLAB_TOKEN"
```

When using Docker Compose, place it in `.env`:

```env
GITCACHE_GITLAB_TOKEN=YOUR_GITLAB_TOKEN
```

---

## Run with Docker

Basic startup:

```bash
docker compose up -d --build
docker logs -f git-cache-gateway
```

Health check:

```bash
curl http://localhost:8080/healthz
```

Status check:

```bash
curl http://localhost:8080/statusz
```

The `/statusz` endpoint reports:

* active background mirror jobs
* submitted jobs
* completed jobs
* failed jobs
* rejected jobs
* queue capacity information

---

## Company / Internal CA Certificates

Some company networks use TLS interception or internal certificate authorities. Without the correct CA certificate, operations such as `pip install`, GitHub access, GitLab API access, or Git mirror operations may fail with certificate verification errors.

Git Cache Gateway supports company/internal CA certificates at both:

1. Docker image build time
2. Container runtime

This is important because `pip install` happens during `docker compose build`, before the runtime entrypoint starts.

### Build-Time CA Support

Put your company or internal CA certificate in the project-root `ca/` directory before building:

```bash
mkdir -p ca
cp company-ca.crt ca/company-ca.crt

docker compose build --no-cache
docker compose up -d
```

The Dockerfile installs any `*.crt` or `*.pem` file from the project-root `ca/` directory into the OS trust store before running:

```bash
python -m pip install --upgrade pip
python -m pip install --no-cache-dir .
```

Important directory layout:

```text
git-cache-gateway/
  ca/
    company-ca.crt
  Dockerfile
  docker-compose.yml
```

The certificate must be under the project-root `ca/` directory, not under `examples/ca/`, because the Docker build context is the project root.

### Runtime CA Support

At runtime, the Docker entrypoint automatically installs any `*.crt` or `*.pem` file mounted under:

```text
/etc/git-cache-gateway/ca
```

Example Compose volume:

```yaml
volumes:
  - ./config.toml:/etc/git-cache-gateway/config.toml:ro
  - ./ca:/etc/git-cache-gateway/ca:ro
```

Recommended TLS configuration:

```toml
[gitlab]
verify_tls = true

[upstream]
verify_tls = true

[tls]
ca_file = ""
ca_path = ""
```

This is the preferred approach because Python, Git, pip, and curl-style HTTPS calls all use the normal public CA store plus your company CA.

### Advanced CA Mode

If you do not want to install the CA into the container OS trust store, you can explicitly point the gateway to a CA file:

```toml
[tls]
ca_file = "/etc/git-cache-gateway/ca/company-ca.crt"
ca_path = ""
```

Use TLS verification disabling only as a temporary diagnostic fallback:

```toml
[gitlab]
verify_tls = false

[upstream]
verify_tls = false
```

---

## Configure a Client

Run:

```bash
scripts/install-client.sh http://git-cache.example.local:8080/
```

This configures Git rewrite rules for:

* `github.com`
* `gitlab.com`
* `bitbucket.org`

The script adds rules for:

* HTTPS syntax
* HTTP syntax
* SSH URL syntax
* SCP-like SSH syntax, such as `git@github.com:org/repo.git`

Example Git rules:

```bash
git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "https://github.com/"
git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "git@github.com:"
git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "ssh://git@github.com/"
```

To configure only GitLab.com:

```bash
scripts/install-client.sh http://git-cache.example.local:8080/ gitlab.com
```

To inspect configured rules:

```bash
git config --global --get-regexp '^url\..*insteadOf'
```

---

## Test the Gateway

Run:

```bash
GIT_TRACE=1 GIT_CURL_VERBOSE=1 git clone https://github.com/octocat/Hello-World.git
```

Git should request the gateway:

```text
http://git-cache.example.local:8080/github.com/octocat/Hello-World.git
```

On first request, the gateway should log a cache miss and proxy the request to GitHub while scheduling a background mirror job.

After the mirror job completes, future requests should be served from the internal GitLab mirror.

---

## Testing Submodules

Use a normal recursive clone:

```bash
git clone --recurse-submodules https://github.com/org/repo.git
```

No special `git-cache` command is required.

If a submodule points to GitHub, GitLab.com, or another configured provider, Git’s `url.insteadOf` rewrite sends the submodule request through the gateway automatically.

You can confirm this with:

```bash
GIT_TRACE=1 GIT_CURL_VERBOSE=1 git submodule update --init --recursive
```

---

## GitLab Runner / CI Usage

GitLab Runner fetches repository sources before the normal job script. Therefore, rewrite rules must be configured before source checkout.

For GitLab CI, use `hooks:pre_get_sources_script`:

```yaml
hooks:
  pre_get_sources_script:
    - git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "https://github.com/"
    - git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "git@github.com:"
    - git config --global --add url."http://git-cache.example.local:8080/github.com/".insteadOf "ssh://git@github.com/"
    - git config --global --add url."http://git-cache.example.local:8080/gitlab.com/".insteadOf "https://gitlab.com/"
    - git config --global --add url."http://git-cache.example.local:8080/gitlab.com/".insteadOf "git@gitlab.com:"
    - git config --global --add url."http://git-cache.example.local:8080/gitlab.com/".insteadOf "ssh://git@gitlab.com/"

variables:
  GIT_SUBMODULE_STRATEGY: recursive
```

If your runner does not apply the hook early enough for your setup, configure the rewrite rules system-wide on the runner host or bake them into the runner image.

---

## Mirror Visibility and Permissions

By default, mirrored repositories and groups are created as GitLab `internal` resources:

```toml
[gitlab]
visibility = "internal"
```

This allows authenticated users on the internal GitLab instance to read the mirror registry, while keeping it unavailable to anonymous users.

The gateway applies this visibility when creating:

* root mirror groups
* provider groups
* nested groups
* mirror projects

It also repairs visibility for repositories that are touched by mirror jobs.

### Enforce Visibility on Existing Mirrors

To recursively repair all existing mirror groups and projects under the configured `root_group`:

```bash
git-cache-gateway enforce-visibility
```

To explicitly choose a visibility:

```bash
git-cache-gateway enforce-visibility --visibility internal
```

By default, this command is best-effort. If GitLab returns `403 Forbidden` for some groups or projects, the command reports skipped objects and continues.

Use stricter modes when needed:

```bash
git-cache-gateway enforce-visibility --fail-on-skipped
git-cache-gateway enforce-visibility --strict
```

Common reasons for `403 Forbidden`:

* the token user is not an Owner of the group
* the token is not from an admin user
* GitLab instance settings restrict `internal` visibility
* a subgroup or project cannot be more visible than its parent group
* company-managed namespace policies block visibility changes

### Visibility Fallback

GitLab may reject `internal` visibility during `POST /projects` or `POST /groups` because of parent namespace settings, instance policy, or token permissions. The gateway preserves the full GitLab API error body in logs so the exact reason is visible.

For resilient operation, keep the mirror functional by allowing a private fallback:

```toml
[gitlab]
visibility = "internal"
visibility_fallback = "private"
strict_visibility = false
```

In this mode, a mirror can still be created as `private` and served through the gateway in `proxy` mode, because the gateway injects GitLab credentials internally. After fixing the GitLab policy or token permissions, run:

```bash
git-cache-gateway enforce-visibility --visibility internal
```

For strict policy enforcement, disable fallback:

```toml
[gitlab]
visibility = "internal"
visibility_fallback = ""
strict_visibility = true
```

---

## Logging

The gateway emits structured logs for request handling and mirror lifecycle events.

Configuration:

```toml
[logging]
level = "INFO"
access_level = "INFO"
request_headers = false
upstream_headers = false
mirror_events = true
```

For verbose debugging:

```toml
[logging]
level = "DEBUG"
access_level = "DEBUG"
request_headers = true
upstream_headers = true
mirror_events = true
```

Sensitive headers such as authorization tokens are redacted.

Example log flow:

```text
event=request_start client=example.local id=12 method=GET path=/github.com/octocat/Hello-World.git/info/refs query=service=git-upload-pack
event=request_map client=example.local id=12 remote=https://github.com/octocat/Hello-World.git mirror=https://gitlab.example.local/mirror/github.com/octocat/Hello-World.git suffix=/info/refs
event=cache_miss client=example.local id=12 remote=https://github.com/octocat/Hello-World.git
mirror_job_submitted remote=https://github.com/octocat/Hello-World.git reason=cache-miss
event=upstream_proxy client=example.local id=12 target=upstream method=GET upstream=https://github.com/octocat/Hello-World.git/info/refs?service=git-upload-pack
event=request_end client=example.local id=12 status=200 target=upstream elapsed_ms=41.3
```

---

## CLI Commands

### Start the server

```bash
git-cache-gateway serve
```

### Check configuration and environment

```bash
git-cache-gateway doctor
```

### Show URL mapping

```bash
git-cache-gateway map https://github.com/org/repo.git
```

### Ensure a mirror manually

```bash
git-cache-gateway ensure https://github.com/org/repo.git
```

### Enforce GitLab visibility

```bash
git-cache-gateway enforce-visibility
```

---

## Important Behavior

* The HTTP server is multi-threaded.
* First-time cache misses do not block clients when `cache_miss_strategy = "proxy_upstream"`.
* Background workers mirror repositories concurrently.
* Duplicate mirror jobs for the same repository are deduplicated in memory.
* Existing internal GitLab mirrors continue working when internet access is down.
* Stale mirror refresh happens in the background when `refresh_existing = true`.
* If the background job queue is full, clients are still served, but the mirror job is rejected and logged.
* Git LFS support is optional and should be tested before production use.

---

## Current Limitations

* This is intended for homelab, LAN, and internal CI usage, not as a public internet proxy.
* Private upstream repositories require upstream credential handling, which is not implemented yet.
* The background job queue is in-memory and is not persisted across container restarts.
* Redirect mode exposes GitLab or upstream URLs directly to clients.
* The gateway does not delete unused mirrors.
* Git LFS support is available as an option but should be validated with your repositories before relying on it.
* Very large monorepos may require tuning worker count, queue size, timeout, and storage capacity.

---

## Recommended Production Checklist

Before using this with multiple users or CI runners:

* Use `proxy` mode.
* Use `cache_miss_strategy = "proxy_upstream"`.
* Configure a persistent cache volume.
* Configure a persistent lock volume.
* Use a GitLab token with the required permissions.
* Install company/internal CA certificates instead of disabling TLS verification.
* Set GitLab mirror visibility intentionally, usually `internal`.
* Run `git-cache-gateway doctor`.
* Test with a small public repository.
* Test with a repository that has submodules.
* Test behavior with internet disconnected after mirrors are created.
* Monitor `/statusz` during CI workloads.
* Keep the gateway accessible only from trusted LAN/VPN networks.

---

## Version Notes

### v0.2.8

- Repairs GitLab mirror `default_branch` on cache-hit readiness checks.
- Fixes clones that downloaded objects successfully but ended with `remote HEAD refers to nonexistent ref`.
- Chooses the mirror default branch from the local upstream mirror when possible, otherwise falls back to an existing `main`, `master`, or the first available branch.

### v0.2.7

- Replaced raw `git push --mirror` with GitLab-safe branch/tag refspec pushes.
- Excludes provider-internal refs such as `refs/merge-requests/*`.
- Skips invalid branch pseudo-refs such as `refs/heads/HEAD`.

### v0.2.5

* Added full GitLab API error bodies and request payloads to mirror job failures.
* Added `visibility_fallback` and `strict_visibility` for GitLab namespace/instance policy compatibility.
* Re-checks whether a project appeared after GitLab returns a project-create error, which helps with races and partially completed previous runs.
* Logs when a mirror falls back from the requested visibility to the actual visibility.

### v0.2.4

* Added build-time CA installation before `pip install`.
* Added shared CA installation script for Docker build and runtime.
* Added `.dockerignore`.
* Ensures company/internal CA certificates are available for:

  * `pip install`
  * Python HTTPS requests
  * gateway proxy HTTPS requests
  * Git mirror operations

### v0.2.3

* Added runtime CA certificate support.
* Docker entrypoint installs mounted CA certificates into the container trust store.
* Added `[tls]` configuration.

### v0.2.2

* `enforce-visibility` became best-effort by default.
* Visibility update failures are reported without crashing.
* Added strict failure options.

### v0.2.1

* Added GitLab.com rewrite support.
* Changed default mirror visibility to `internal`.
* Added visibility creation and repair through GitLab API.

### v0.2.0

* Added threaded HTTP server.
* Added non-blocking cache-miss behavior.
* Added background mirror queue.
* Added worker pool.
* Added `/statusz`.

---

## License

Add your project license here.
