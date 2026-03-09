#!/usr/bin/env bash

set -euo pipefail
shopt -s nullglob

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_FILE="$SCRIPT_DIR/device_broadcast.cpp"
OUT_DIR="$SCRIPT_DIR/dist"
APP_NAME="device_broadcast"
CXXFLAGS=(-std=c++11 -O2 -Wall -Wextra)

declare -A DISCOVERED_COMPILERS=()
declare -a DISCOVERED_COMPILER_NAMES=()
declare -a SEARCH_DIRS=()

mkdir -p "$OUT_DIR"

usage() {
    cat <<'EOF'
Usage:
  ./build.sh
  ./build.sh --list-compilers
EOF
}

append_search_dir() {
    local dir="$1"
    local existing

    [[ -n "$dir" && -d "$dir" ]] || return 0
    for existing in "${SEARCH_DIRS[@]:-}"; do
        [[ "$existing" == "$dir" ]] && return 0
    done
    SEARCH_DIRS+=("$dir")
}

collect_search_dirs() {
    local path_entry

    IFS=':' read -r -a PATH_ENTRIES <<< "${PATH:-}"
    for path_entry in "${PATH_ENTRIES[@]}"; do
        append_search_dir "$path_entry"
    done

    for path_entry in \
        "/home/axera/gcc-arm-"*"/bin" \
        "/home/axera/gcc-linaro-"*"/bin" \
        "/home/axera/arm-AX620E-linux-uclibcgnueabihf/bin" \
        "/opt/"*"/bin"
    do
        append_search_dir "$path_entry"
    done
}

register_compiler() {
    local compiler_path="$1"
    local compiler_name

    [[ -x "$compiler_path" ]] || return 0
    compiler_name="$(basename "$compiler_path")"

    case "$compiler_name" in
        gcc|g++|c++|*-gcc|*-g++)
            ;;
        *)
            return 0
            ;;
    esac

    [[ -n "${DISCOVERED_COMPILERS[$compiler_name]:-}" ]] && return 0
    DISCOVERED_COMPILERS["$compiler_name"]="$compiler_path"
    DISCOVERED_COMPILER_NAMES+=("$compiler_name")
}

sort_compiler_names() {
    local sorted=()
    if [[ "${#DISCOVERED_COMPILER_NAMES[@]}" -eq 0 ]]; then
        return 0
    fi
    mapfile -t sorted < <(printf '%s\n' "${DISCOVERED_COMPILER_NAMES[@]}" | sort -u)
    DISCOVERED_COMPILER_NAMES=("${sorted[@]}")
}

discover_compilers() {
    local dir
    local candidate

    collect_search_dirs
    for dir in "${SEARCH_DIRS[@]}"; do
        for candidate in \
            "$dir"/g++ \
            "$dir"/c++ \
            "$dir"/gcc \
            "$dir"/*-g++ \
            "$dir"/*-gcc
        do
            register_compiler "$candidate"
        done
    done
    sort_compiler_names
}

print_compilers() {
    local compiler_name
    local compiler_path
    local version_line

    echo "Detected compilers:"
    if [[ "${#DISCOVERED_COMPILER_NAMES[@]}" -eq 0 ]]; then
        echo "  (none)"
        return 0
    fi

    for compiler_name in "${DISCOVERED_COMPILER_NAMES[@]}"; do
        compiler_path="${DISCOVERED_COMPILERS[$compiler_name]}"
        version_line="$("$compiler_path" --version 2>/dev/null | head -n 1 || true)"
        if [[ -n "$version_line" ]]; then
            printf '  %s -> %s :: %s\n' "$compiler_name" "$compiler_path" "$version_line"
        else
            printf '  %s -> %s\n' "$compiler_name" "$compiler_path"
        fi
    done
}

find_compiler_by_name() {
    local compiler_name

    for compiler_name in "$@"; do
        if [[ -n "${DISCOVERED_COMPILERS[$compiler_name]:-}" ]]; then
            printf '%s\n' "${DISCOVERED_COMPILERS[$compiler_name]}"
            return 0
        fi
    done
    return 1
}

find_compiler_by_regex() {
    local regex="$1"
    local suffix
    local compiler_name

    for suffix in "g++" "gcc"; do
        for compiler_name in "${DISCOVERED_COMPILER_NAMES[@]}"; do
            [[ "$compiler_name" == *"$suffix" ]] || continue
            [[ "$compiler_name" =~ $regex ]] || continue
            printf '%s\n' "${DISCOVERED_COMPILERS[$compiler_name]}"
            return 0
        done
    done
    return 1
}

find_compiler() {
    local regex="$1"
    shift

    if find_compiler_by_name "$@" >/dev/null 2>&1; then
        find_compiler_by_name "$@"
        return 0
    fi

    [[ -n "$regex" ]] || return 1
    find_compiler_by_regex "$regex"
}

compile_cpp() {
    local compiler_path="$1"
    local output_path="$2"
    local compiler_name

    compiler_name="$(basename "$compiler_path")"
    if [[ "$compiler_name" == *gcc && "$compiler_name" != *g++ ]]; then
        "$compiler_path" "${CXXFLAGS[@]}" -o "$output_path" "$SRC_FILE" -lstdc++
    else
        "$compiler_path" "${CXXFLAGS[@]}" -o "$output_path" "$SRC_FILE"
    fi
}

build_target() {
    local label="$1"
    local output_path="$2"
    local regex="$3"
    shift 3

    local compiler_path
    local compiler_name

    if ! compiler_path="$(find_compiler "$regex" "$@")"; then
        echo "[skip] $label: no matching compiler found"
        return 0
    fi

    compiler_name="$(basename "$compiler_path")"
    echo "[build] $label -> $(basename "$output_path") (compiler: $compiler_name)"
    compile_cpp "$compiler_path" "$output_path"
}

main() {
    local mode="${1:-build}"
    local native_arch

    case "$mode" in
        build|"")
            ;;
        --list-compilers)
            discover_compilers
            print_compilers
            return 0
            ;;
        -h|--help)
            usage
            return 0
            ;;
        *)
            usage >&2
            return 1
            ;;
    esac

    discover_compilers
    print_compilers
    echo

    native_arch="$(uname -m)"
    build_target "native" "$OUT_DIR/${APP_NAME}-${native_arch}-linux-gnu" '^((x86_64|aarch64|arm|riscv64|loongarch64).*-)?(g\+\+|gcc)$' \
        g++ c++ gcc
    build_target "aarch64 glibc" "$OUT_DIR/${APP_NAME}-aarch64-linux-gnu" '^aarch64[-_].*linux.*gnu-(g\+\+|gcc)$' \
        aarch64-none-linux-gnu-g++ aarch64-linux-gnu-g++ aarch64-none-linux-gnu-gcc aarch64-linux-gnu-gcc
    build_target "armv7 glibc" "$OUT_DIR/${APP_NAME}-armv7-linux-gnueabihf" '^arm([-_].*)?linux.*gnueabihf-(g\+\+|gcc)$' \
        arm-linux-gnueabihf-g++ arm-linux-gnueabihf-gcc
    build_target "ax620e uclibc" "$OUT_DIR/${APP_NAME}-armv7-ax620e-uclibc" '^arm[-_].*[Aa][Xx]620[Ee].*uclibc.*-(g\+\+|gcc)$' \
        arm-AX620E-linux-uclibcgnueabihf-g++ arm-AX620E-linux-uclibcgnueabihf-gcc

    echo
    echo "Build artifacts:"
    find "$OUT_DIR" -maxdepth 1 -type f -name "${APP_NAME}-*" -printf '  %f\n' | sort
}

main "${1:-build}"
