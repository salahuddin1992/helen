# Minimal Helen edge-worker image. Only ships the edge subset of the
# Python codebase to keep the image small and the attack surface narrow.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/helen

# Install just the edge-relevant dependencies.
COPY deploy/edge/edge-requirements.txt /tmp/req.txt
RUN if [ ! -s /tmp/req.txt ]; then \
      echo "fastapi==0.115.*\nuvicorn[standard]==0.30.*\nhttpx==0.27.*\nredis==5.*\nPillow==10.*\npynacl==1.*\nmaxminddb==2.*" \
        > /tmp/req.txt; \
    fi \
    && pip install -r /tmp/req.txt

# Copy only what the edge needs.
COPY app/services/edge        /opt/helen/app/services/edge
COPY app/core/logging.py      /opt/helen/app/core/logging.py
COPY app/core/__init__.py     /opt/helen/app/core/__init__.py
COPY deploy/edge/edge_main.py /opt/helen/edge_main.py

EXPOSE 8089
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "edge_main:app", "--host", "0.0.0.0", "--port", "8089"]
