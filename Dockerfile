# Build with python3.14 on trixie
# (match the tag with version in pyproject.toml)
# `docker build -t proxy-md-converter:<tag> .`

FROM python:3.14-slim-trixie AS builder
# Get uv
COPY --from=ghcr.io/astral-sh/uv:0.9.18-python3.14-trixie-slim /uv /bin/uv

WORKDIR /app

COPY pyproject.toml .python-version ./
RUN echo "# placeholder" > README.md

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv \
    && uv lock \
    && uv sync --no-dev

FROM python:3.14-slim-trixie

WORKDIR /app

COPY --from=builder /app/.venv ./
COPY README.md ./
COPY src/ ./src/

# Add uvicorn in path
ENV PATH="/app/.venv/bin:${PATH}"

ENTRYPOINT ["uvicorn", "proxy_md_converter.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
