#!/bin/bash

set -e

python -m alembic upgrade head &
migration_pid=$!
trap 'kill -TERM "$migration_pid" 2>/dev/null || true; wait "$migration_pid" || true; exit 143' TERM INT
wait "$migration_pid"
trap - TERM INT
exec python -m bin.api
