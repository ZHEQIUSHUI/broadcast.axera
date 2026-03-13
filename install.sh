#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_NAME="device_broadcast"
SOURCE_FILE="$SCRIPT_DIR/device_broadcast.cpp"
BUILD_DIR="$SCRIPT_DIR/dist"
SERVICE_NAME="${APP_NAME}.service"
INIT_SCRIPT_NAME="S90${APP_NAME}"
TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t device_broadcast.XXXXXX)"
PERSISTENCE_MODE="unknown"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup 0 INT TERM HUP

CURRENT_USER="$(id -un)"
DEFAULT_USER="${DEVICE_BROADCAST_USER:-${SUDO_USER:-$(logname 2>/dev/null || echo "$CURRENT_USER")}}"
if ! id "$DEFAULT_USER" >/dev/null 2>&1; then
    DEFAULT_USER="$CURRENT_USER"
fi
TARGET_USER="$DEFAULT_USER"
TARGET_GROUP="$(id -gn "$TARGET_USER" 2>/dev/null || echo "$TARGET_USER")"

if [ "$(id -u)" -eq 0 ]; then
    INSTALL_BIN_DIR="${INSTALL_BIN_DIR:-/usr/bin}"
    STATE_DIR="${STATE_DIR:-/var/lib/${APP_NAME}}"
    LOG_DIR="${LOG_DIR:-/var/log/${APP_NAME}}"
else
    INSTALL_BIN_DIR="${INSTALL_BIN_DIR:-$HOME/.local/bin}"
    STATE_DIR="${STATE_DIR:-$HOME/.local/share/${APP_NAME}}"
    LOG_DIR="${LOG_DIR:-$HOME/.local/state/${APP_NAME}}"
fi

CRON_MARKER="# broadcast.axera ${APP_NAME}"
PACKAGE_VERSION="${DEVICE_BROADCAST_PACKAGE_VERSION:-}"

log() {
    printf '%s\n' "$*"
}

write_version_file() {
    TARGET_PATH="$1"
    [ -n "$PACKAGE_VERSION" ] || return 0
    [ -n "$TARGET_PATH" ] || return 0

    TARGET_DIR="$(dirname "$TARGET_PATH")"
    mkdir -p "$TARGET_DIR" >/dev/null 2>&1 || return 0
    printf '%s\n' "$PACKAGE_VERSION" > "$TARGET_PATH" 2>/dev/null || return 0
}

prepare_writable_dir() {
    TARGET_DIR="$1"
    mkdir -p "$TARGET_DIR" >/dev/null 2>&1 || return 1
    TEST_FILE="$TARGET_DIR/.device_broadcast_write_test_$$"
    touch "$TEST_FILE" >/dev/null 2>&1 || return 1
    rm -f "$TEST_FILE"
    return 0
}

select_writable_dir() {
    PRIMARY_DIR="$1"
    shift

    if prepare_writable_dir "$PRIMARY_DIR"; then
        printf '%s\n' "$PRIMARY_DIR"
        return 0
    fi

    for CANDIDATE_DIR in "$@"; do
        if [ -n "$CANDIDATE_DIR" ] && prepare_writable_dir "$CANDIDATE_DIR"; then
            printf '%s\n' "$CANDIDATE_DIR"
            return 0
        fi
    done

    return 1
}

select_install_layout() {
    if [ "$(id -u)" -eq 0 ]; then
        INSTALL_BIN_DIR="$(select_writable_dir "$INSTALL_BIN_DIR" \
            "/customer/bin" \
            "/customer/dell/bin" \
            "/tmp/${APP_NAME}/bin")" || {
            log "No writable binary directory found."
            exit 1
        }

        STATE_DIR="$(select_writable_dir "$STATE_DIR" \
            "/customer/${APP_NAME}" \
            "/customer/dell/${APP_NAME}" \
            "/tmp/${APP_NAME}/state")" || {
            log "No writable state directory found."
            exit 1
        }

        LOG_DIR="$(select_writable_dir "$LOG_DIR" \
            "/var/log/${APP_NAME}" \
            "/customer/${APP_NAME}/log" \
            "/customer/dell/${APP_NAME}/log" \
            "/tmp/${APP_NAME}/log")" || {
            log "No writable log directory found."
            exit 1
        }
    else
        INSTALL_BIN_DIR="$(select_writable_dir "$INSTALL_BIN_DIR" "/tmp/${APP_NAME}/bin")" || {
            log "No writable binary directory found."
            exit 1
        }
        STATE_DIR="$(select_writable_dir "$STATE_DIR" "/tmp/${APP_NAME}/state")" || {
            log "No writable state directory found."
            exit 1
        }
        LOG_DIR="$(select_writable_dir "$LOG_DIR" "/tmp/${APP_NAME}/log")" || {
            log "No writable log directory found."
            exit 1
        }
    fi

    APP_PATH="$INSTALL_BIN_DIR/$APP_NAME"
    RUNNER_PATH="$STATE_DIR/${APP_NAME}_runner.sh"
    PID_FILE="$STATE_DIR/${APP_NAME}.pid"
    LOG_FILE="$LOG_DIR/${APP_NAME}.log"
}

detect_device_kind() {
    if [ -d /proc/ax_proc ]; then
        echo "ax"
        return
    fi

    if [ -r /proc/device-tree/model ] && grep -aq "Raspberry Pi" /proc/device-tree/model; then
        echo "raspberry_pi"
        return
    fi

    DETECT_ARCH="$(uname -m)"
    case "$DETECT_ARCH" in
        x86_64|amd64|i386|i686)
            echo "x86"
            ;;
        aarch64|arm64|armv7l|armv8l|arm*)
            echo "arm_linux"
            ;;
        *)
            echo "generic_linux"
            ;;
    esac
}

detect_libc() {
    if DETECT_LINE="$(ldd --version 2>&1 | head -n 1)"; then
        DETECT_LINE="$(printf '%s' "$DETECT_LINE" | tr '[:upper:]' '[:lower:]')"
        case "$DETECT_LINE" in
            *uclibc*)
                echo "uclibc"
                return
                ;;
            *musl*)
                echo "musl"
                return
                ;;
            *glibc*|*"gnu libc"*)
                echo "glibc"
                return
                ;;
        esac
    fi

    if ls /lib/libuClibc-* >/dev/null 2>&1; then
        echo "uclibc"
        return
    fi

    echo "unknown"
}

select_prebuilt_binary() {
    SELECT_ARCH="$(uname -m)"
    SELECT_LIBC="$(detect_libc)"
    SELECT_KIND="$(detect_device_kind)"

    case "${SELECT_KIND}:${SELECT_ARCH}:${SELECT_LIBC}" in
        ax:armv7l:uclibc|ax:arm*:uclibc)
            SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-armv7-ax620e-uclibc"
            ;;
        ax:armv7l:*|ax:arm*:*)
            if [ -f "$BUILD_DIR/${APP_NAME}-armv7-linux-gnueabihf" ]; then
                SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-armv7-linux-gnueabihf"
            else
                SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-armv7-ax620e-uclibc"
            fi
            ;;
        raspberry_pi:aarch64:*|arm_linux:aarch64:*|arm_linux:arm64:*)
            SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-aarch64-linux-gnu"
            ;;
        raspberry_pi:armv7l:*|raspberry_pi:arm*:*)
            SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-armv7-linux-gnueabihf"
            ;;
        arm_linux:armv7l:*|arm_linux:arm*:*)
            if [ "$SELECT_LIBC" = "uclibc" ] && [ -f "$BUILD_DIR/${APP_NAME}-armv7-ax620e-uclibc" ]; then
                SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-armv7-ax620e-uclibc"
            else
                SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-armv7-linux-gnueabihf"
            fi
            ;;
        x86:x86_64:*|generic_linux:x86_64:*)
            SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-x86_64-linux-gnu"
            ;;
        *)
            SELECT_CANDIDATE="$BUILD_DIR/${APP_NAME}-${SELECT_ARCH}-linux-gnu"
            ;;
    esac

    [ -f "$SELECT_CANDIDATE" ] || return 1
    printf '%s\n' "$SELECT_CANDIDATE"
}

build_native_binary() {
    BUILD_OUTPUT="$TMP_DIR/$APP_NAME"
    [ -f "$SOURCE_FILE" ] || return 1
    command -v g++ >/dev/null 2>&1 || return 1
    g++ -std=c++11 -O2 -Wall -Wextra -o "$BUILD_OUTPUT" "$SOURCE_FILE"
    printf '%s\n' "$BUILD_OUTPUT"
}

ensure_paths() {
    mkdir -p "$INSTALL_BIN_DIR" "$STATE_DIR" "$LOG_DIR"

    if [ "$(id -u)" -eq 0 ] && [ "$TARGET_USER" != "root" ]; then
        chown -R "$TARGET_USER:$TARGET_GROUP" "$STATE_DIR" "$LOG_DIR"
    fi
}

write_version_markers() {
    [ -n "$PACKAGE_VERSION" ] || return 0

    write_version_file "$STATE_DIR/version"
    write_version_file "$APP_PATH.version"

    if [ "$(id -u)" -eq 0 ]; then
        write_version_file "/etc/${APP_NAME}.version"
    fi
}

install_binary() {
    SOURCE_BINARY="$1"
    cp "$SOURCE_BINARY" "$APP_PATH"
    chmod 755 "$APP_PATH"
}

write_runner_script() {
    cat > "$RUNNER_PATH" <<EOF
#!/bin/sh

APP_PATH="$APP_PATH"
RUN_USER="$TARGET_USER"
PID_FILE="$PID_FILE"
LOG_FILE="$LOG_FILE"

is_running() {
    [ -f "\$PID_FILE" ] || return 1
    pid=\$(cat "\$PID_FILE" 2>/dev/null || true)
    [ -n "\$pid" ] || return 1
    kill -0 "\$pid" 2>/dev/null
}

start_app() {
    if is_running; then
        echo "already running"
        return 0
    fi

    mkdir -p "\$(dirname "\$PID_FILE")" "\$(dirname "\$LOG_FILE")"

    if [ "\$RUN_USER" = "root" ]; then
        nohup "\$APP_PATH" >> "\$LOG_FILE" 2>&1 &
        echo \$! > "\$PID_FILE"
    else
        su -s /bin/sh -c "nohup '\$APP_PATH' >> '\$LOG_FILE' 2>&1 & echo \\\$! > '\$PID_FILE'" "\$RUN_USER"
    fi

    sleep 1
    if ! is_running; then
        echo "failed to start \$APP_PATH" >&2
        return 1
    fi
}

stop_app() {
    if is_running; then
        kill "\$(cat "\$PID_FILE")" 2>/dev/null || true
        sleep 1
    fi
    rm -f "\$PID_FILE"
}

case "\${1:-start}" in
    start)
        start_app
        ;;
    stop)
        stop_app
        ;;
    restart)
        stop_app
        start_app
        ;;
    status)
        if is_running; then
            echo "running"
        else
            echo "stopped"
            exit 1
        fi
        ;;
    *)
        echo "usage: \$0 {start|stop|restart|status}"
        exit 1
        ;;
esac
EOF
    chmod 755 "$RUNNER_PATH"
}

stop_existing_instances() {
    if [ -x "$RUNNER_PATH" ]; then
        "$RUNNER_PATH" stop >/dev/null 2>&1 || true
    fi

    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
        systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi

    pkill -f "$APP_PATH" >/dev/null 2>&1 || true
    pkill -x "$APP_NAME" >/dev/null 2>&1 || true
    pkill -f "/customer/dell/broadcast.axera/$APP_NAME" >/dev/null 2>&1 || true
    killall "$APP_NAME" >/dev/null 2>&1 || true
}

can_use_systemd_system() {
    [ "$(id -u)" -eq 0 ] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    [ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" = "systemd" ] || return 1
    [ -d /etc/systemd/system ] || return 1
    prepare_writable_dir /etc/systemd/system
}

can_use_systemd_user() {
    [ "$(id -u)" -ne 0 ] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user show-environment >/dev/null 2>&1
}

can_use_initd() {
    [ "$(id -u)" -eq 0 ] || return 1
    [ -d /etc/init.d ] || return 1
    prepare_writable_dir /etc/init.d
}

can_use_rc_local() {
    [ "$(id -u)" -eq 0 ] || return 1
    prepare_writable_dir /etc
}

install_systemd_system() {
    UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Generic Broadcast Device Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
Group=$TARGET_GROUP
ExecStart=$APP_PATH
Restart=always
RestartSec=5
WorkingDirectory=$INSTALL_BIN_DIR
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME"
    PERSISTENCE_MODE="systemd-system"
}

install_systemd_user() {
    UNIT_DIR="$HOME/.config/systemd/user"
    UNIT_PATH="$UNIT_DIR/$SERVICE_NAME"
    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Generic Broadcast Device Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$APP_PATH
Restart=always
RestartSec=5
WorkingDirectory=$INSTALL_BIN_DIR
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    PERSISTENCE_MODE="systemd-user"
}

install_init_script() {
    INIT_PATH="/etc/init.d/$INIT_SCRIPT_NAME"

    cat > "$INIT_PATH" <<EOF
#!/bin/sh

RUNNER="$RUNNER_PATH"

case "\${1:-start}" in
    start|stop|restart|status)
        exec "\$RUNNER" "\$1"
        ;;
    *)
        echo "usage: \$0 {start|stop|restart|status}"
        exit 1
        ;;
esac
EOF

    chmod 755 "$INIT_PATH"

    if command -v update-rc.d >/dev/null 2>&1; then
        update-rc.d "$INIT_SCRIPT_NAME" defaults 90 >/dev/null 2>&1 || true
    elif command -v chkconfig >/dev/null 2>&1; then
        chkconfig --add "$INIT_SCRIPT_NAME" >/dev/null 2>&1 || true
    fi

    "$INIT_PATH" restart
    PERSISTENCE_MODE="init.d"
}

install_rc_local() {
    RC_LOCAL="/etc/rc.local"
    RUNNER_LINE="sh '$RUNNER_PATH' start >/dev/null 2>&1 ${CRON_MARKER}"

    if [ ! -f "$RC_LOCAL" ]; then
        cat > "$RC_LOCAL" <<'EOF'
#!/bin/sh
exit 0
EOF
        chmod 755 "$RC_LOCAL"
    fi

    if ! grep -Fq "$CRON_MARKER" "$RC_LOCAL"; then
        sed -i "\$i $RUNNER_LINE" "$RC_LOCAL"
    fi

    "$RUNNER_PATH" restart
    PERSISTENCE_MODE="rc.local"
}

install_crontab_persistence() {
    command -v crontab >/dev/null 2>&1 || return 1

    CURRENT_CRONTAB="$(crontab -l 2>/dev/null || true)"
    CURRENT_CRONTAB="$(printf '%s\n' "$CURRENT_CRONTAB" | grep -Fv "$CRON_MARKER" || true)"
    printf '%s\n%s\n' "$CURRENT_CRONTAB" "@reboot sh '$RUNNER_PATH' start >/dev/null 2>&1 $CRON_MARKER" | crontab -
    "$RUNNER_PATH" restart
    PERSISTENCE_MODE="crontab"
}

install_nohup_only() {
    "$RUNNER_PATH" restart
    PERSISTENCE_MODE="nohup"
}

main() {
    log "Installing $APP_NAME ..."
    log "Detected run user: $TARGET_USER"
    log "Detected device kind: $(detect_device_kind), arch: $(uname -m), libc: $(detect_libc)"

    select_install_layout
    log "Install dirs: bin=$INSTALL_BIN_DIR state=$STATE_DIR log=$LOG_DIR"

    ensure_paths

    if BINARY_SOURCE="$(build_native_binary 2>/dev/null)"; then
        log "Native binary compiled with g++."
        log "Using freshly compiled native binary."
    elif BINARY_SOURCE="$(select_prebuilt_binary 2>/dev/null)"; then
        log "Using prebuilt binary: $(basename "$BINARY_SOURCE")"
    else
        log "No native compiler and no matching prebuilt binary found."
        log "Run ./build.sh on a machine with the required toolchains first."
        exit 1
    fi

    write_runner_script
    stop_existing_instances
    install_binary "$BINARY_SOURCE"

    if can_use_systemd_system; then
        install_systemd_system
    elif can_use_systemd_user; then
        install_systemd_user
    elif can_use_initd; then
        install_init_script
    elif can_use_rc_local; then
        install_rc_local
    elif install_crontab_persistence; then
        :
    else
        install_nohup_only
    fi

    write_version_markers

    log
    log "Install completed."
    log "Binary: $APP_PATH"
    log "Runner: $RUNNER_PATH"
    log "Log file: $LOG_FILE"
    log "Persistence: $PERSISTENCE_MODE"
    if [ -n "$PACKAGE_VERSION" ]; then
        log "Package version: $PACKAGE_VERSION"
    fi
}

main "$@"
