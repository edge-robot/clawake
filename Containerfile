# Devcontainer for py-clawake-dev, owned by onshape-architekt.
# Clawake is a host-side CLI (uv tool / venv), so there is no runtime stage.
# The image deliberately contains no podman, systemd or openclaw: the agent can run
# unit tests and previews, never touch a real deployment.
#
#   podman build --target dev -t localhost/clawake-dev:latest .

FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM docker.io/library/node:24-trixie-slim AS dev
ARG CLAUDE_CODE_VERSION=2.1.300
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates ripgrep less procps python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
COPY .devcontainer/hooks/ /opt/devcontainer/hooks/
# Use the distro Python (>=3.11 required by pyproject) and keep the venv out of the
# bind-mounted repo so host and container environments never collide.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PROJECT_ENVIRONMENT=/home/dev/.venvs/clawake \
    UV_LINK_MODE=copy
WORKDIR /workspaces/clawake
CMD ["bash"]
