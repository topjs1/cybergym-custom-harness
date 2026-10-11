FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HARNESS_WORK_ROOT=/data/tasks \
    HARNESS_RESULTS_CSV=/data/results.csv \
    HARNESS_EVENTS_JSONL=/data/events.jsonl

RUN addgroup --system --gid 10001 harness \
    && adduser --system --uid 10001 --ingroup harness --home /home/harness harness \
    && mkdir -p /app /data/tasks \
    && chown -R harness:harness /app /data

WORKDIR /app
COPY --chown=harness:harness pyproject.toml uv.lock README.md ./
COPY --chown=harness:harness src ./src
RUN uv sync --locked --no-dev

USER 10001:10001
EXPOSE 9009
ENTRYPOINT ["/app/.venv/bin/python", "-m", "cybergym_harness.server"]
CMD ["--host", "0.0.0.0", "--port", "9009"]
