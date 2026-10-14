# syntax=docker/dockerfile:1
#
# Baton Agent Orchestrator — dashboard image.
#
#   docker build -t jev-dashboard .
#   docker run --rm -p 127.0.0.1:8765:8765 -v ./my-project:/data jev-dashboard
#
# Serves `jev dashboard` for the git repo mounted at /data. Configure with:
#   JEV_HOST            bind address (default 0.0.0.0, so the port leaves the
#                       container; the dashboard still requires a token then)
#   JEV_PORT            dashboard port (default 8765)
#   JEV_DASHBOARD_TOKEN access token (min 16 chars). Off loopback a token is
#                       required; when unset, jev generates one at startup and
#                       prints the URL in the container logs (`docker logs`).
#
# No secrets are baked into the image. Runs as the non-root user `jev`.
# See docs/packaging.md for the compose example and remote-access guidance.

FROM python:3.12-slim-bookworm AS builder

WORKDIR /build
# Only what the wheel build needs, for good layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir '.[dashboard]'

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 jev

COPY --from=builder /usr/local /usr/local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JEV_HOST=0.0.0.0 \
    JEV_PORT=8765

WORKDIR /data
USER jev

EXPOSE 8765

# With a token set, an authenticated GET proves the app itself serves: a valid
# token gets a 303 (it is swapped for a cookie); anything else is a 401.
# Without a token (jev generates one at startup) a TCP probe only proves
# uvicorn is listening.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD sh -c 'if [ -n "$JEV_DASHBOARD_TOKEN" ]; then curl -fsS -o /dev/null "http://127.0.0.1:${JEV_PORT:-8765}/?token=${JEV_DASHBOARD_TOKEN}"; else python3 -c "import os, socket; s = socket.create_connection((\"127.0.0.1\", int(os.environ.get(\"JEV_PORT\", \"8765\"))), timeout=5); s.close()"; fi'

CMD ["sh", "-c", "exec jev dashboard --no-open --repo /data --host \"${JEV_HOST:-0.0.0.0}\" --port \"${JEV_PORT:-8765}\""]
