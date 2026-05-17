# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.12
ARG BASE_REGISTRY=docker.io

FROM ${BASE_REGISTRY}/python:${PYTHON_VERSION}-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-cache --extra ansible --extra ai --extra otel


FROM ${BASE_REGISTRY}/python:${PYTHON_VERSION}-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_HOST_KEY_CHECKING=False

RUN useradd -m -u 1001 rocannon
USER rocannon

ENTRYPOINT ["rocannon"]
