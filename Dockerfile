# Build with python3.14 on trixie
# (match the tag with version in pyproject.toml)
# `docker build -t MarkGate:<tag> .`

FROM python:3.14-slim-trixie AS builder
# Setup uv
COPY --from=ghcr.io/astral-sh/uv:0.9.18-python3.14-trixie-slim /usr/local/bin/uv /bin/uv
# optionally config uv mirror repo

WORKDIR /app

COPY pyproject.toml .python-version ./
RUN echo "# placeholder" > README.md

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv \
    && uv lock \
    && uv sync --no-dev

FROM python:3.14-slim-trixie

COPY --from=builder /app/.venv /app/.venv
COPY README.md /app/
COPY src/ /app/src/

# Add uvicorn in path
ENV PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

ENTRYPOINT ["uvicorn", "src.proxy_md_converter.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
