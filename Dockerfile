# syntax=docker/dockerfile:1

# ---- build stage: produce a wheel -------------------------------------------
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
RUN pip install build

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

# ---- runtime stage -----------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KEYFINDER_HOST=0.0.0.0 \
    KEYFINDER_PORT=8000 \
    KEYFINDER_DATA_DIR=/data

COPY --from=build /dist/*.whl /tmp/wheels/
RUN pip install /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && groupadd --system --gid 10001 keyfinder \
    && useradd --system --uid 10001 --gid keyfinder --home-dir /data --shell /usr/sbin/nologin keyfinder \
    && mkdir -p /data \
    && chown keyfinder:keyfinder /data

USER keyfinder
WORKDIR /data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["keyfinder", "serve"]
