# Build with python3.14 on trixie
# (match the tag with version in pyproject.toml)
# `docker build -f docker/Dockerfile -t markgate:<tag> .`

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

ENV PATH="/app/.venv/bin:${PATH}"


RUN apt update \
    && apt upgrade \
    && apt install -y --no-install-recommends \
        libmagic1 \
    && apt autoremove -y \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
#COPY README.md /app/
COPY src/markgate/ /app/

# Add uvicorn in path


WORKDIR /app

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0"]
CMD ["--port", "8080"]
