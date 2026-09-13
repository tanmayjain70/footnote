#!/bin/sh
# Container entrypoint: migrate, serve, then seed.
#
# The seed runs behind the server rather than before it. Ingesting the demo
# corpus means embedding a few thousand passages on a small CPU, which takes
# minutes; a platform health check that waits for that would declare the
# container dead. So uvicorn starts first and the seeder fills the database
# while the API is already answering. Every later boot finds the data present
# and the seeder exits in a second.
#
# `set -e` matters: without it a failed migration would be logged and the API
# would start anyway against a stale schema, which is worse than not starting.
set -e

echo "==> Running database migrations"
alembic upgrade head

PORT="${PORT:-8000}"

if [ "${DEMO_MODE}" = "true" ]; then
    (
        echo "==> Waiting for the API before seeding"
        tries=0
        until python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/api/v1/healthz', timeout=2)" >/dev/null 2>&1; do
            tries=$((tries + 1))
            if [ "$tries" -gt 60 ]; then
                echo "==> The API did not come up; seeding anyway"
                break
            fi
            sleep 2
        done
        python -m app.demo.seed || echo "==> Seeding skipped or failed; the API is still up"
    ) &
fi

echo "==> Starting API on port ${PORT}"
# exec so uvicorn becomes PID 1 and receives SIGTERM directly. Without it the
# shell holds PID 1, swallows the signal, and every deploy waits for the
# platform's kill timeout instead of shutting down cleanly.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers --forwarded-allow-ips '*'
