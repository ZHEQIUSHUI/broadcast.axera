#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_NAME="${IMAGE_NAME:-broadcast-axera-dashboard}"
IMAGE_TAG="${IMAGE_TAG:-local}"
IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"

CONTAINER_NAME="${CONTAINER_NAME:-broadcast-dashboard}"
NETWORK_MODE="${NETWORK_MODE:-host}" # host | bridge

RUNTIME_DIR="${RUNTIME_DIR:-${REPO_ROOT}/.runtime_docker}"
DASHBOARD_PORT="${DASHBOARD_PORT:-25000}"
WEBSSH2_LISTEN_PORT="${WEBSSH2_LISTEN_PORT:-2222}"
WEBSSH2_ENABLED="${WEBSSH2_ENABLED:-1}"
DASHBOARD_SITE_LABEL="${DASHBOARD_SITE_LABEL:-本地(Docker)}"

DOWNLOAD_TOOLCHAINS="${DOWNLOAD_TOOLCHAINS:-0}"

WEBSSH2_URL_TEMPLATE="${WEBSSH2_URL_TEMPLATE:-}"
if [[ -z "${WEBSSH2_URL_TEMPLATE}" ]]; then
  WEBSSH2_URL_TEMPLATE="http://{dashboard_host}:${WEBSSH2_LISTEN_PORT}/ssh/host/{host}"
fi

log() {
  printf '%s\n' "$*"
}

warn() {
  printf '[warn] %s\n' "$*" >&2
}

port_in_use_tcp() {
  local port="$1"
  ss -ltnH "sport = :${port}" 2>/dev/null | head -n 1 | grep -q .
}

port_in_use_udp() {
  local port="$1"
  ss -lunH "sport = :${port}" 2>/dev/null | head -n 1 | grep -q .
}

log "[update] repo: ${REPO_ROOT}"
log "[update] image: ${IMAGE}"
log "[update] container: ${CONTAINER_NAME}"

mkdir -p "${RUNTIME_DIR}"

if systemctl is-active --quiet dashboard.service 2>/dev/null; then
  warn "dashboard.service is active (systemd). If both run, UDP 9999 traffic may be split. Consider: sudo systemctl disable --now dashboard.service"
fi

if [[ "${NETWORK_MODE}" == "host" ]]; then
  if port_in_use_tcp "${DASHBOARD_PORT}"; then
    warn "TCP port ${DASHBOARD_PORT} is already in use; container may fail to start. You can set DASHBOARD_PORT=25002 (or stop the process)."
  fi
  if port_in_use_tcp "${WEBSSH2_LISTEN_PORT}"; then
    warn "TCP port ${WEBSSH2_LISTEN_PORT} is already in use; webssh2 may fail to start. You can set WEBSSH2_LISTEN_PORT=2223."
  fi
  if port_in_use_udp 9999; then
    warn "UDP port 9999 is already in use. This is OK only if the other process also sets SO_REUSEADDR; otherwise container may fail to bind."
  fi
else
  warn "NETWORK_MODE=${NETWORK_MODE}. If you can't see devices, switch to NETWORK_MODE=host (UDP broadcast is often not delivered via bridge)."
fi

log "[update] docker build..."
docker build \
  --build-arg "DOWNLOAD_TOOLCHAINS=${DOWNLOAD_TOOLCHAINS}" \
  -t "${IMAGE}" \
  "${REPO_ROOT}"

log "[update] stopping old container (if any)..."
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

log "[update] starting container..."
if [[ "${NETWORK_MODE}" == "host" ]]; then
  docker run -d \
    --name "${CONTAINER_NAME}" \
    --network host \
    --restart unless-stopped \
    -e "DASHBOARD_PORT=${DASHBOARD_PORT}" \
    -e "WEBSSH2_ENABLED=${WEBSSH2_ENABLED}" \
    -e "WEBSSH2_LISTEN_PORT=${WEBSSH2_LISTEN_PORT}" \
    -e "WEBSSH2_URL_TEMPLATE=${WEBSSH2_URL_TEMPLATE}" \
    -e "DASHBOARD_SITE_LABEL=${DASHBOARD_SITE_LABEL}" \
    -v "${RUNTIME_DIR}:/app/.runtime" \
    "${IMAGE}" >/dev/null
else
  docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    -p "${DASHBOARD_PORT}:25000" \
    -p "${WEBSSH2_LISTEN_PORT}:2222" \
    -p "9999:9999/udp" \
    -e "WEBSSH2_ENABLED=${WEBSSH2_ENABLED}" \
    -e "WEBSSH2_LISTEN_PORT=2222" \
    -e "WEBSSH2_URL_TEMPLATE=${WEBSSH2_URL_TEMPLATE}" \
    -e "DASHBOARD_SITE_LABEL=${DASHBOARD_SITE_LABEL}" \
    -v "${RUNTIME_DIR}:/app/.runtime" \
    "${IMAGE}" >/dev/null
fi

log "[update] done"
log "  Dashboard: http://127.0.0.1:${DASHBOARD_PORT}"
if [[ "${WEBSSH2_ENABLED}" != "0" ]]; then
  log "  WebSSH2:   http://127.0.0.1:${WEBSSH2_LISTEN_PORT}"
fi
log
log "Logs:"
log "  docker logs -f ${CONTAINER_NAME}"
