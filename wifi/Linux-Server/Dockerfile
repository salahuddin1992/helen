# Helen-Server — container image.
# Two-stage: build once on a fat image, ship from python:slim.
# Build:  docker build -t helen-server .
# Run:    docker run -d --name helen-server -p 3000:3000 -p 3443:3443 \
#           -v helen-data:/var/lib/helen -v helen-logs:/var/log/helen \
#           helen-server

# ── Stage 1: build PyInstaller bundle ──────────────────────────
FROM python:3.12-bookworm AS build

WORKDIR /src
COPY CommClient-Server/ ./CommClient-Server/
WORKDIR /src/CommClient-Server

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir pyinstaller && \
    pyinstaller --noconfirm CommClient-Server.spec

# ── Stage 2: runtime image ─────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --system --home-dir /var/lib/helen --shell /usr/sbin/nologin \
            --user-group helen && \
    mkdir -p /var/lib/helen /var/log/helen /etc/helen && \
    chown -R helen:helen /var/lib/helen /var/log/helen

COPY --from=build /src/CommClient-Server/dist/Helen-Server /opt/helen/server
COPY Linux-Server/config/helen-server.env /etc/helen/server.env

RUN chown -R helen:helen /opt/helen

USER helen
WORKDIR /var/lib/helen

ENV PORT=3000 \
    HELEN_HOST=0.0.0.0 \
    HELEN_HTTPS_DISABLED=1 \
    COMMCLIENT_DATA_DIR=/var/lib/helen \
    COMMCLIENT_LOG_DIR=/var/log/helen

EXPOSE 3000 3443 41234/udp 5353/udp

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/api/health || exit 1

ENTRYPOINT ["/opt/helen/server/Helen-Server"]
