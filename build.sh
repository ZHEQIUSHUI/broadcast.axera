#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_FILE="$SCRIPT_DIR/device_broadcast.cpp"
OUT_DIR="$SCRIPT_DIR/dist"
APP_NAME="device_broadcast"
CXXFLAGS=(-std=c++11 -O2 -Wall -Wextra)

mkdir -p "$OUT_DIR"

find_compiler() {
    local compiler="$1"
    local candidate
    if candidate="$(command -v "$compiler" 2>/dev/null)"; then
        printf '%s\n' "$candidate"
        return 0
    fi

    for candidate in \
        "/home/axera/gcc-arm-9.2-2019.12-x86_64-aarch64-none-linux-gnu/bin/$compiler" \
        "/home/axera/gcc-linaro-7.5.0-2019.12-x86_64_arm-linux-gnueabihf/bin/$compiler" \
        "/home/axera/arm-AX620E-linux-uclibcgnueabihf/bin/$compiler"
    do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

build_target() {
    local label="$1"
    local compiler="$2"
    local output="$3"
    local compiler_path

    if ! compiler_path="$(find_compiler "$compiler")"; then
        echo "[skip] $label: $compiler not found"
        return 0
    fi

    echo "[build] $label -> $(basename "$output")"
    "$compiler_path" "${CXXFLAGS[@]}" -o "$output" "$SRC_FILE"
}

native_arch="$(uname -m)"

build_target "native" "g++" "$OUT_DIR/${APP_NAME}-${native_arch}-linux-gnu"
build_target "aarch64 glibc" "aarch64-none-linux-gnu-g++" "$OUT_DIR/${APP_NAME}-aarch64-linux-gnu"
build_target "armv7 glibc" "arm-linux-gnueabihf-g++" "$OUT_DIR/${APP_NAME}-armv7-linux-gnueabihf"
build_target "ax620e uclibc" "arm-AX620E-linux-uclibcgnueabihf-g++" "$OUT_DIR/${APP_NAME}-armv7-ax620e-uclibc"

echo
echo "Build artifacts:"
find "$OUT_DIR" -maxdepth 1 -type f -name "${APP_NAME}-*" -printf '  %f\n' | sort
