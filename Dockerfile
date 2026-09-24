FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY scripts/vendor.mjs scripts/vendor.mjs
RUN npm run vendor

FROM denoland/deno:bin-2.7.5 AS deno
FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data PORT=8000 PATH="/app/.venv/bin:$PATH" DENO_DIR=/tmp/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates tzdata intel-media-va-driver mesa-va-drivers tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 tube && useradd -r -u 10001 -g tube -d /app tube \
    && mkdir -p /data && chown tube:tube /data
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python
COPY app ./app
COPY --from=frontend /build/app/static/vendor ./app/static/vendor
USER tube
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/healthz', timeout=2)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-access-log"]
