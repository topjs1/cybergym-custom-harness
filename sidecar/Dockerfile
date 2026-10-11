FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HARNESS_DYNAMIC_ANALYSIS=inline \
    HARNESS_WORK_ROOT=/data/tasks

# The sidecar needs the Docker CLI to spawn builder/target containers.
# It is the designated privilege boundary: it holds the Docker socket but no
# API key and no egress. The Purple harness never receives the socket.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /data/tasks

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY sidecar ./sidecar
RUN uv sync --locked --no-dev

EXPOSE 9110
ENTRYPOINT ["/app/.venv/bin/python", "-m", "sidecar.app"]
