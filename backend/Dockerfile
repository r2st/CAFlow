# CAFlow backend — API, Celery worker and beat all run from this one image.
# Which of them a container becomes is decided by the entrypoint argument, so
# the three processes can never drift apart in their dependencies.

# --- build ------------------------------------------------------------------

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# A venv rather than the system site-packages: the runtime stage copies this
# one directory and inherits nothing else from the build.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt ./
# Every dependency ships a manylinux wheel, so no compiler is needed here —
# which is also why the runtime image can stay this small.
RUN pip install --no-cache-dir -r requirements.txt

# --- runtime ----------------------------------------------------------------

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    STORAGE_DIR=/var/lib/caflow/documents

# Uploaded documents are written at runtime, so the directory has to belong to
# the unprivileged user the container actually runs as.
RUN useradd --create-home --uid 10001 caflow \
    && mkdir -p /var/lib/caflow/documents \
    && chown -R caflow:caflow /var/lib/caflow

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=caflow:caflow alembic.ini ./
COPY --chown=caflow:caflow alembic ./alembic
COPY --chown=caflow:caflow app ./app
COPY --chown=caflow:caflow docker-entrypoint.sh /usr/local/bin/caflow-entrypoint

RUN chmod +x /usr/local/bin/caflow-entrypoint

USER caflow
EXPOSE 8000

# Liveness only: readiness pulls in the database, and a database blip should
# take the instance out of the load balancer, not restart every container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"

ENTRYPOINT ["caflow-entrypoint"]
CMD ["api"]
