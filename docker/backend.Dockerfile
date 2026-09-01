# Shared image for both the "backend" and "scheduler" docker-compose
# services -- same codebase, different command (api.main vs scheduler.loop).
# See docker-compose.yml, DECISIONS.md (issue #52).

FROM python:3.12-slim

# uv, copied from its own official distroless image rather than installed
# via pip/curl -- the documented, fastest way to get a pinned uv binary
# into a Dockerfile. See https://docs.astral.sh/uv/guides/integration/docker/.
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency files copied and synced before the rest of the source tree,
# so editing application code doesn't invalidate this (expensive) layer --
# only a pyproject.toml/uv.lock change does. --no-dev excludes
# pytest/black/ruff, which the runtime image never needs. --frozen refuses
# to update uv.lock itself, matching CI-style reproducible installs.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# `uv run` at container-start would re-resolve/re-sync against every
# dependency group (pulling black/ruff back in, and needing network
# access on every single container start) -- verified directly. Putting
# the already-built venv on PATH and invoking python directly instead
# uses exactly what was locked and installed above, with no runtime
# network dependency at all. See DECISIONS.md,
# https://docs.astral.sh/uv/guides/integration/docker/.
ENV PATH="/app/.venv/bin:$PATH"

# Now the actual application code.
COPY agent/ agent/
COPY api/ api/
COPY config/ config/
COPY extractors/ extractors/
COPY pipeline/ pipeline/
COPY providers/ providers/
COPY scheduler/ scheduler/
COPY storage/ storage/

# Overwritten by docker-compose.yml's RUNNING_IN_DOCKER=true for both
# services -- see config/settings.py::EnvSettings.running_in_docker.
# Backend and scheduler both bind no host-side dialogs, so no browser/GUI
# dependency is needed in this image at all (agent/browse.py's PowerShell
# call is unreachable in Docker -- see api/routes/settings.py's guard).

EXPOSE 8080

CMD ["python", "-m", "api.main"]
