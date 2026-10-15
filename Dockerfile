FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_VERSION=0.13.0 \
    PATH="/app/.venv/bin:/usr/local/bin:/usr/bin:/bin"

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates make \
    && python -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN uv sync --locked --no-dev

CMD ["make", "test"]
