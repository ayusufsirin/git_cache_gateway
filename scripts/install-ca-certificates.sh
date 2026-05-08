#!/bin/sh
set -eu

# Install extra CA certificates into the Debian/Ubuntu trust store.
# Accepts one optional source directory argument. Defaults to /etc/git-cache-gateway/ca.
CA_SRC_DIR="${1:-${GITCACHE_EXTRA_CA_DIR:-/etc/git-cache-gateway/ca}}"
CA_DST_DIR="/usr/local/share/ca-certificates/git-cache-gateway"

if [ ! -d "$CA_SRC_DIR" ]; then
  exit 0
fi

found=0
mkdir -p "$CA_DST_DIR"

for cert in "$CA_SRC_DIR"/*.crt "$CA_SRC_DIR"/*.pem; do
  [ -f "$cert" ] || continue
  found=1
  base="$(basename "$cert")"
  case "$base" in
    *.pem) base="${base%.pem}.crt" ;;
  esac
  cp "$cert" "$CA_DST_DIR/$base"
done

if [ "$found" = "1" ]; then
  echo "Installing extra CA certificates from $CA_SRC_DIR"
  update-ca-certificates
fi
