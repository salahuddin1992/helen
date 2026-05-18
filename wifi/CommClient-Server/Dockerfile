# Dockerfile — production helen-server image.
#
# Distinct from Dockerfile.chaos, which is the lab/iteration image:
#
#   * Multi-stage build: a builder layer compiles wheels, the runtime
#     layer installs from those wheels with no compiler toolchain
#     baggage. Final image ~250 MB instead of ~700 MB.
#   * Non-root runtime user (uid 1000:1000).
#   * No dev / test extras; pytest, httpx etc. are stripped.
#   * Healthcheck identical to the chaos image (we still want
#     orchestrators to know when we're up).
#
# Production rollout assumes Redis + (optional) NATS + S3/MinIO are
# external services. This Dockerfile bundles the helen-server only.
# Set HELEN_REDIS_URL, HELEN_ENV=production at deploy time.

# ── Builder stage ────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build deps required for some wheels (cryptography, msgpack, etc.).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev \
        rustc \
        cargo \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt /build/requirements.txt

# Build wheels into a stage-local cache so the runtime layer can
# pip install --no-build-isolation against them.
RUN pip wheel --wheel-dir /build/wheels -r requirements.txt


# ── Runtime stage ────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HELEN_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000 \
    LOG_LEVEL=INFO

# Non-root runtime user for least-privilege ops.
RUN groupadd --system --gid 1000 helen \
 && useradd  --system --uid 1000 --gid helen --shell /sbin/nologin --home-dir /app helen

# Minimal runtime libs — no compiler toolchain.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install from pre-built wheels (no network, no compilers).
COPY --from=builder /build/wheels /tmp/wheels
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-build-isolation \
        --find-links /tmp/wheels -r /tmp/requirements.txt \
 && rm -rf /tmp/wheels /tmp/requirements.txt

# App source. Order: alembic + app + run.py last so ordinary code
# changes don't bust the layer with dependencies.
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY app /app/app
COPY run.py /app/run.py

# Writable data dir for the per-user state path the app expects.
RUN mkdir -p /app/data /app/logs \
 && chown -R helen:helen /app

USER helen:helen
EXPOSE 3000

# Healthcheck identical to chaos image.
HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=20s \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=2).status == 200 else 1)"

# Run via run.py — same entry point as chaos. The lifespan handler
# logs `redis_client_connected` etc. so operators can verify config.
CMD ["python", "run.py"]
