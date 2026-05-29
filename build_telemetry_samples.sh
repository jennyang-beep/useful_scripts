#!/bin/bash
#
# build_telemetry_samples.sh
#
# Build every DOCA telemetry sample under a samples root by running
# `meson setup meson-build` followed by `ninja -C meson-build` in each
# subdirectory that contains a meson.build.
#
# Usage:
#   ./build_telemetry_samples.sh                       # build all samples
#   ./build_telemetry_samples.sh -r /custom/path       # use a custom samples root
#   ./build_telemetry_samples.sh -s telemetry_phy      # only build one sample
#   ./build_telemetry_samples.sh -c                    # clean meson-build dirs first
#   ./build_telemetry_samples.sh -j 8                  # ninja parallelism (default: auto)
#
# Default samples root: /opt/mellanox/doca/samples/doca_telemetry
#
# Requires: meson, ninja, cmake, pkg-config, libjson-c-dev.
# On Ubuntu/Debian, any missing dependencies are auto-installed via apt-get.
#

set -uo pipefail

SAMPLES_ROOT="/opt/mellanox/doca/samples/doca_telemetry"
BUILD_DIR_NAME="meson-build"
ONLY_SAMPLE=""
CLEAN=0
NINJA_JOBS=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [-r SAMPLES_ROOT] [-s SAMPLE] [-c] [-j JOBS] [-h]

Options:
  -r SAMPLES_ROOT  Root directory containing telemetry samples
                   (default: ${SAMPLES_ROOT})
  -s SAMPLE        Only build the named sample subdirectory (e.g. telemetry_phy).
                   May be passed multiple times to select several samples.
  -c               Remove any existing '${BUILD_DIR_NAME}' directory before
                   running 'meson setup' (clean reconfigure).
  -j JOBS          Number of parallel ninja jobs (default: let ninja decide).
  -h               Show this help message.

Description:
  Iterates every immediate subdirectory of SAMPLES_ROOT that contains a
  meson.build file. For each one, runs:
      meson setup ${BUILD_DIR_NAME}
      ninja -C ${BUILD_DIR_NAME}
  Prints a per-sample status line and a final summary. Exit code is non-zero
  if any sample fails to build.
EOF
}

declare -a SELECTED_SAMPLES=()

while getopts ":r:s:cj:h" opt; do
    case "$opt" in
        r) SAMPLES_ROOT="$OPTARG" ;;
        s) SELECTED_SAMPLES+=("$OPTARG") ;;
        c) CLEAN=1 ;;
        j) NINJA_JOBS="$OPTARG" ;;
        h) usage; exit 0 ;;
        \?) echo "Error: unknown option -$OPTARG" >&2; usage; exit 1 ;;
        :)  echo "Error: option -$OPTARG requires an argument" >&2; usage; exit 1 ;;
    esac
done

is_ubuntu() {
    command -v apt-get >/dev/null 2>&1
}

# Install a list of apt packages, using sudo only when not already root.
apt_install() {
    local sudo_cmd=""
    if [[ "$(id -u)" -ne 0 ]]; then
        if command -v sudo >/dev/null 2>&1; then
            sudo_cmd="sudo"
        else
            echo "Error: need root (or sudo) to install: $*" >&2
            return 1
        fi
    fi
    echo "  installing: $*"
    $sudo_cmd apt-get update -y && $sudo_cmd apt-get install -y "$@"
}

# Ensure build dependencies are present. On Ubuntu/Debian, auto-install any
# that are missing; otherwise just fail with a helpful message.
ensure_dependencies() {
    declare -a missing_pkgs=()

    # command -> apt package name
    command -v meson      >/dev/null 2>&1 || missing_pkgs+=("meson")
    command -v ninja      >/dev/null 2>&1 || missing_pkgs+=("ninja-build")
    command -v cmake      >/dev/null 2>&1 || missing_pkgs+=("cmake")
    command -v pkg-config >/dev/null 2>&1 || missing_pkgs+=("pkg-config")

    # json-c is a library dependency, detected via pkg-config.
    if ! pkg-config --exists json-c >/dev/null 2>&1; then
        missing_pkgs+=("libjson-c-dev")
    fi

    if [[ ${#missing_pkgs[@]} -eq 0 ]]; then
        return 0
    fi

    echo "Missing dependencies: ${missing_pkgs[*]}"

    if is_ubuntu; then
        if ! apt_install "${missing_pkgs[@]}"; then
            echo "Error: failed to install dependencies: ${missing_pkgs[*]}" >&2
            exit 1
        fi
    else
        echo "Error: not an apt-based system; please install manually: ${missing_pkgs[*]}" >&2
        exit 1
    fi

    # Re-verify the tools and json-c after installation.
    for cmd in meson ninja cmake pkg-config; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            echo "Error: required command '$cmd' still not found after install" >&2
            exit 1
        fi
    done
    if ! pkg-config --exists json-c >/dev/null 2>&1; then
        echo "Error: json-c still not detected by pkg-config after install" >&2
        echo "Hint: if installed in a non-standard prefix, set PKG_CONFIG_PATH to the dir with json-c.pc" >&2
        exit 1
    fi
}

ensure_dependencies

if [[ ! -d "$SAMPLES_ROOT" ]]; then
    echo "Error: samples root '$SAMPLES_ROOT' does not exist or is not a directory" >&2
    exit 1
fi

# Build the list of sample directories to process.
declare -a sample_dirs=()
if [[ ${#SELECTED_SAMPLES[@]} -gt 0 ]]; then
    for name in "${SELECTED_SAMPLES[@]}"; do
        dir="${SAMPLES_ROOT}/${name}"
        if [[ ! -d "$dir" ]]; then
            echo "Error: requested sample '${name}' not found under ${SAMPLES_ROOT}" >&2
            exit 1
        fi
        if [[ ! -f "${dir}/meson.build" ]]; then
            echo "Error: '${dir}' has no meson.build" >&2
            exit 1
        fi
        sample_dirs+=("$dir")
    done
else
    while IFS= read -r -d '' dir; do
        sample_dirs+=("$dir")
    done < <(find "$SAMPLES_ROOT" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
fi

if [[ ${#sample_dirs[@]} -eq 0 ]]; then
    echo "No sample subdirectories found under ${SAMPLES_ROOT}" >&2
    exit 1
fi

declare -a succeeded=()
declare -a failed=()
declare -a skipped=()

ninja_args=()
if [[ -n "$NINJA_JOBS" ]]; then
    ninja_args+=("-j" "$NINJA_JOBS")
fi

for dir in "${sample_dirs[@]}"; do
    name="$(basename "$dir")"

    if [[ ! -f "${dir}/meson.build" ]]; then
        echo "----- skip ${name} (no meson.build) -----"
        skipped+=("$name")
        continue
    fi

    echo "===== building ${name} ====="
    build_path="${dir}/${BUILD_DIR_NAME}"

    if [[ "$CLEAN" -eq 1 && -d "$build_path" ]]; then
        echo "  removing existing ${build_path}"
        rm -rf "$build_path"
    fi

    if [[ -d "$build_path" ]]; then
        echo "  reconfiguring existing ${BUILD_DIR_NAME}"
        if ! meson setup --reconfigure "$build_path" "$dir"; then
            echo "Error: meson reconfigure failed for ${name}" >&2
            failed+=("$name")
            continue
        fi
    else
        if ! ( cd "$dir" && meson setup "$BUILD_DIR_NAME" ); then
            echo "Error: meson setup failed for ${name}" >&2
            failed+=("$name")
            continue
        fi
    fi

    if ! ninja -C "$build_path" "${ninja_args[@]}"; then
        echo "Error: ninja build failed for ${name}" >&2
        failed+=("$name")
        continue
    fi

    succeeded+=("$name")
done

echo
echo "===== build summary ====="
echo "succeeded (${#succeeded[@]}):"
for n in "${succeeded[@]}"; do echo "  - $n"; done
echo "failed (${#failed[@]}):"
for n in "${failed[@]}"; do echo "  - $n"; done
if [[ ${#skipped[@]} -gt 0 ]]; then
    echo "skipped (${#skipped[@]}):"
    for n in "${skipped[@]}"; do echo "  - $n"; done
fi

if [[ ${#failed[@]} -gt 0 ]]; then
    exit 1
fi
exit 0
