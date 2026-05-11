#!/usr/bin/env sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.test.yml}"
SERVICE="${SERVICE:-git-cache-gateway-tests}"
SKIP_BUILD="${SKIP_BUILD:-0}"

if [ "$SKIP_BUILD" != "1" ]; then
    docker compose -f "$COMPOSE_FILE" build "$SERVICE"
fi

if [ "$#" -eq 0 ]; then
    exec docker compose -f "$COMPOSE_FILE" run --rm "$SERVICE"
fi

exec docker compose -f "$COMPOSE_FILE" run --rm "$SERVICE" pytest "$@"
