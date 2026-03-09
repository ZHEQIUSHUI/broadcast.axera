#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEGACY_SERVICE_NAME="dashboard.service"
MODERN_SERVICE_NAME="broadcast_dashboard.service"
CURRENT_USER="$(id -un)"
DEFAULT_USER="${BROADCAST_DASHBOARD_USER:-${SUDO_USER:-$(logname 2>/dev/null || echo "$CURRENT_USER")}}"
if ! id "$DEFAULT_USER" >/dev/null 2>&1; then
    DEFAULT_USER="$CURRENT_USER"
fi
TARGET_USER="$DEFAULT_USER"
TARGET_GROUP="$(id -gn "$TARGET_USER" 2>/dev/null || echo "$TARGET_USER")"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

resolve_service_name() {
    if [[ -n "${DASHBOARD_SERVICE_NAME:-}" ]]; then
        printf '%s\n' "$DASHBOARD_SERVICE_NAME"
        return
    fi

    if [[ -f "/etc/systemd/system/$LEGACY_SERVICE_NAME" ]] || [[ -f "$HOME/.config/systemd/user/$LEGACY_SERVICE_NAME" ]]; then
        printf '%s\n' "$LEGACY_SERVICE_NAME"
        return
    fi

    if [[ -f "/etc/systemd/system/$MODERN_SERVICE_NAME" ]] || [[ -f "$HOME/.config/systemd/user/$MODERN_SERVICE_NAME" ]]; then
        printf '%s\n' "$MODERN_SERVICE_NAME"
        return
    fi

    printf '%s\n' "$LEGACY_SERVICE_NAME"
}

SERVICE_NAME="$(resolve_service_name)"

if [[ -z "$PYTHON_BIN" ]]; then
    echo "python3 not found"
    exit 1
fi

if [[ "$(id -u)" -eq 0 ]]; then
    LOG_DIR="${LOG_DIR:-/var/log/broadcast_dashboard}"
else
    LOG_DIR="${LOG_DIR:-$HOME/.local/state/broadcast_dashboard}"
fi

RUNNER_DIR="$SCRIPT_DIR/.runtime"
RUNNER_PATH="$RUNNER_DIR/run_dashboard.sh"
LOG_FILE="$LOG_DIR/dashboard.log"
CRON_MARKER="# broadcast.axera dashboard"
PERSISTENCE_MODE="unknown"

mkdir -p "$RUNNER_DIR" "$LOG_DIR"
if [[ "$(id -u)" -eq 0 ]] && [[ "$TARGET_USER" != "root" ]]; then
    chown -R "$TARGET_USER:$TARGET_GROUP" "$RUNNER_DIR" "$LOG_DIR"
fi

cat > "$RUNNER_PATH" <<EOF
#!/bin/sh

PYTHON_BIN="$PYTHON_BIN"
DASHBOARD_SCRIPT="$SCRIPT_DIR/dashboard.py"
LOG_FILE="$LOG_FILE"

case "\${1:-start}" in
    start)
        nohup "\$PYTHON_BIN" "\$DASHBOARD_SCRIPT" >> "\$LOG_FILE" 2>&1 &
        ;;
    *)
        echo "usage: \$0 start"
        exit 1
        ;;
esac
EOF
chmod 755 "$RUNNER_PATH"

can_use_systemd_system() {
    [[ "$(id -u)" -eq 0 ]] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" == "systemd" ]]
}

can_use_systemd_user() {
    [[ "$(id -u)" -ne 0 ]] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user show-environment >/dev/null 2>&1
}

install_systemd_system() {
    cat > "/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=Broadcast Device Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
Group=$TARGET_GROUP
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON_BIN $SCRIPT_DIR/dashboard.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME"
    if [[ "$SERVICE_NAME" != "$LEGACY_SERVICE_NAME" ]]; then
        systemctl disable --now "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$SERVICE_NAME" != "$MODERN_SERVICE_NAME" ]]; then
        systemctl disable --now "$MODERN_SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    PERSISTENCE_MODE="systemd-system"
}

install_systemd_user() {
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/$SERVICE_NAME" <<EOF
[Unit]
Description=Broadcast Device Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON_BIN $SCRIPT_DIR/dashboard.py
Restart=always
RestartSec=5
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    if [[ "$SERVICE_NAME" != "$LEGACY_SERVICE_NAME" ]]; then
        systemctl --user disable --now "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$SERVICE_NAME" != "$MODERN_SERVICE_NAME" ]]; then
        systemctl --user disable --now "$MODERN_SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    PERSISTENCE_MODE="systemd-user"
}

install_crontab() {
    command -v crontab >/dev/null 2>&1 || return 1
    local current
    current="$(crontab -l 2>/dev/null || true)"
    current="$(printf '%s\n' "$current" | grep -Fv "$CRON_MARKER" || true)"
    printf '%s\n%s\n' "$current" "@reboot sh '$RUNNER_PATH' start >/dev/null 2>&1 $CRON_MARKER" | crontab -
    sh "$RUNNER_PATH" start
    PERSISTENCE_MODE="crontab"
}

install_nohup() {
    sh "$RUNNER_PATH" start
    PERSISTENCE_MODE="nohup"
}

if can_use_systemd_system; then
    install_systemd_system
elif can_use_systemd_user; then
    install_systemd_user
elif install_crontab; then
    :
else
    install_nohup
fi

echo "Dashboard install completed."
echo "Run user: $TARGET_USER"
echo "Python: $PYTHON_BIN"
echo "Log file: $LOG_FILE"
echo "Service: $SERVICE_NAME"
echo "Persistence: $PERSISTENCE_MODE"
echo "URL: http://<dashboard-ip>:25000"
