#!/usr/bin/env sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.test.yml}"
SERVICE="${SERVICE:-git-cache-gateway-tests}"

if [ "$#" -eq 0 ]; then
    exec docker compose -f "$COMPOSE_FILE" run --rm "$SERVICE"
fi

exec docker compose -f "$COMPOSE_FILE" run --rm "$SERVICE" pytest "$@"
