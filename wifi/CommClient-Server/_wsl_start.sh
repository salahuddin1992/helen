#!/bin/bash
# Transient launcher — deleted after use.
export PORT=3099
export HELEN_HTTPS_DISABLED=1
export COMMCLIENT_DATA_DIR=/tmp/helen-linux-data
export COMMCLIENT_LOG_DIR=/tmp/helen-linux-logs
mkdir -p "$COMMCLIENT_DATA_DIR" "$COMMCLIENT_LOG_DIR"
cd "$(dirname "$0")"
exec python3 run.py
