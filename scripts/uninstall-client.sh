#!/usr/bin/env bash
set -euo pipefail

# Remove all url.*.insteadOf entries that point to a git-cache gateway.
# Review output first if you have other insteadOf rules.
git config --global --get-regexp '^url\..*\.insteadOf' || true

echo "Manual removal example:"
echo '  git config --global --unset-all url.http://git-cache.example.local:8080/github.com/.insteadOf'
