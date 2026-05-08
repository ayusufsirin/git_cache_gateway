#!/usr/bin/env bash
set -euo pipefail

GATEWAY_URL="${1:-}"
if [[ -z "$GATEWAY_URL" ]]; then
  echo "Usage: $0 http://git-cache.example.local:8080/ [host ...]" >&2
  echo "Default hosts: github.com gitlab.com bitbucket.org" >&2
  exit 2
fi
shift || true
GATEWAY_URL="${GATEWAY_URL%/}/"

if [[ "$#" -gt 0 ]]; then
  HOSTS=("$@")
else
  HOSTS=(github.com gitlab.com bitbucket.org)
fi

add_rule() {
  local base="$1"
  local prefix="$2"
  if git config --global --get-all "url.${base}.insteadOf" | grep -Fxq "$prefix"; then
    echo "Already configured: ${prefix} -> ${base}"
  else
    git config --global --add "url.${base}.insteadOf" "$prefix"
    echo "Configured: ${prefix} -> ${base}"
  fi
}

for host in "${HOSTS[@]}"; do
  base="${GATEWAY_URL}${host}/"
  add_rule "$base" "https://${host}/"
  add_rule "$base" "http://${host}/"
  add_rule "$base" "ssh://git@${host}/"
  add_rule "$base" "git@${host}:"
done

cat <<MSG

Current rewrite rules:
$(git config --global --get-regexp '^url\..*\.insteadOf' || true)
MSG
