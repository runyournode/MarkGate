# Build with python3.14 on trixie
# (match the tag with version in pyproject.toml)
# from root folder:  `docker build -f Dockerfile -t markgate:<tag> .`

FROM python:3.14-slim-trixie AS builder
# Setup uv
COPY --from=ghcr.io/astral-sh/uv:0.10.6-python3.14-trixie-slim /usr/local/bin/uv /bin/uv
# optionally config uv mirror repo

WORKDIR /app

COPY pyproject.toml .python-version ./
RUN echo "# placeholder" > README.md

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv \
    && uv lock \
    && uv sync --no-dev

FROM python:3.14-slim-trixie


RUN groupadd -g 1000 app && \
    useradd -u 1000 -g app -m -s /bin/bash app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt update \
    && apt upgrade \
    && apt install -y --no-install-recommends \
        libmagic1 \
    && apt autoremove -y \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/markgate/ /app/

# Pre-compile bytecode
RUN python -m compileall -q -f /app \
    && chown -R app:app /app

# Add uvicorn in path
ENV PATH="/app/.venv/bin:${PATH}"

# Running the app as non root
USER app
WORKDIR /app

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0"]
CMD ["--port", "8080"]
