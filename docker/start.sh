#!/usr/bin/env bash

set -euo pipefail

WEBSSH2_ENABLED="${WEBSSH2_ENABLED:-1}"
WEBSSH2_LISTEN_PORT="${WEBSSH2_LISTEN_PORT:-2222}"

if [[ "${WEBSSH2_ENABLED}" != "0" ]]; then
  echo "[webssh2] starting on 0.0.0.0:${WEBSSH2_LISTEN_PORT}"
  (
    cd /opt/webssh2
    exec node dist/index.js
  ) &
fi

echo "[dashboard] starting on 0.0.0.0:${DASHBOARD_PORT:-25000}"
exec python dashboard.py

