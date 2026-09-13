# Footnote API
#
# Docker rather than a host's native Python runtime, for two reasons:
#
#   1. The Python version is pinned here rather than in a hosting provider's
#      settings, so a local build and a deployed build are the same build.
#   2. It keeps the app portable. Moving off Render later is a change of
#      hosting, not a rewrite of the deployment.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FASTEMBED_CACHE_DIR=/app/.fastembed_cache

WORKDIR /app

# Dependencies first, in their own layer. Application code changes far more
# often than requirements.txt, so this keeps rebuilds fast.
#
# psycopg[binary] ships its own libpq, so no system Postgres client is needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image. Without this every cold start on a
# free instance downloads 65 MB before it can ingest or answer anything, and
# the first request after a sleep would time out. Embeddings are computed here,
# in the container; nothing about a document leaves it for this step.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/app/.fastembed_cache')"

COPY . .

# Git does not record the executable bit on Windows checkouts, so the entrypoint
# arrives as 0644 and Docker cannot exec it. The container then dies instantly
# with status 128 and no message explaining why. Setting the bit here makes the
# image correct no matter how the file was checked out.
RUN chmod +x /app/docker-entrypoint.sh

# Run as a non-root user. Nothing here needs root, and a container that does not
# need it should not have it.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Render supplies $PORT; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
