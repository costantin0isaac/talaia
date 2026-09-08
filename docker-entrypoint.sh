#!/bin/sh
# Apply migrations before the server binds, so the schema is never behind the code.
set -eu

echo "talaia: applying database migrations"
alembic upgrade head

echo "talaia: starting"
exec "$@"
