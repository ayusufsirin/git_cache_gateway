#!/bin/sh
set -eu

/app/scripts/install-ca-certificates.sh "${GITCACHE_EXTRA_CA_DIR:-/etc/git-cache-gateway/ca}"

exec "$@"
