#!/bin/sh
# Container entrypoint: migrate, then serve.
#
# Seeding is NOT started here. It used to be: a second `python -m app.demo.seed`
# in the background, waiting for the port. That loads a second copy of the
# embedding model -- about 270 MB resident -- and on a 512 MB instance the pair
# is killed before either finishes, over and over. The application seeds itself
# on a thread instead (DEMO_MODE, see app/main.py), so one process holds one
# model and the seeding shares it with the requests it is filling the database
# for.
#
# `set -e` matters: without it a failed migration would be logged and the API
# would start anyway against a stale schema, which is worse than not starting.
set -e

echo "==> Running database migrations"
alembic upgrade head

PORT="${PORT:-8000}"

echo "==> Starting API on port ${PORT}"
# exec so uvicorn becomes PID 1 and receives SIGTERM directly. Without it the
# shell holds PID 1, swallows the signal, and every deploy waits for the
# platform's kill timeout instead of shutting down cleanly.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers --forwarded-allow-ips '*'
