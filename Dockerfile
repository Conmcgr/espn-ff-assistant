# Shadow worker image for Cloud Run Jobs. Secrets arrive as environment
# variables from Secret Manager at run time; none are baked into the image.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra gcs --no-install-project
COPY src ./src
COPY scripts ./scripts
COPY migrations ./migrations
RUN uv sync --locked --extra gcs && useradd --system --uid 10001 worker \
    && mkdir -p data/raw && chown worker data/raw
USER worker
ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["python"]
CMD ["scripts/scheduled_tick.py"]
