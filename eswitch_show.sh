#!/bin/bash
#
# eswitch_show.sh
#
# Discover BlueField-4 (BF4) and/or ConnectX-9 (CX9) PCI addresses via
# `mst status -v` and run `devlink dev eswitch show pci/<addr>` for each.
#
# Usage:
#   ./eswitch_show.sh                 # both BF4 and CX9
#   ./eswitch_show.sh -d BF4          # only BlueField-4
#   ./eswitch_show.sh -d CX9          # only ConnectX-9
#   ./eswitch_show.sh -p              # just print the PCI addresses (no devlink)
#
# Requires: mst, devlink, awk, and read access to /sys/bus/pci/devices.
# Typically must be run as root.
#

set -uo pipefail

DEVICE_FILTER="both"
PRINT_ONLY=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [-d DEVICE_TYPE] [-p] [-h]

Options:
  -d DEVICE_TYPE   Device family to query: BF4 | CX9 | both  (default: both)
  -p               Only print the discovered PCI addresses; don't run devlink
  -h               Show this help message

Description:
  Parses 'mst status -v' for BlueField4 (BF4) and/or ConnectX9 (CX9) devices,
  resolves each to a full 'domain:bus:dev.fn' PCI address via sysfs, then runs
  'devlink dev eswitch show pci/<addr>' for each match.
EOF
}

while getopts ":d:ph" opt; do
    case "$opt" in
        d) DEVICE_FILTER="$OPTARG" ;;
        p) PRINT_ONLY=1 ;;
        h) usage; exit 0 ;;
        \?) echo "Error: unknown option -$OPTARG" >&2; usage; exit 1 ;;
        :)  echo "Error: option -$OPTARG requires an argument" >&2; usage; exit 1 ;;
    esac
done

case "$DEVICE_FILTER" in
    BF4|CX9|both) ;;
    *)
        echo "Error: invalid device type '$DEVICE_FILTER' (expected BF4, CX9, or both)" >&2
        exit 1
        ;;
esac

for cmd in mst awk; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Error: required command '$cmd' not found in PATH" >&2
        exit 1
    fi
done
if [[ "$PRINT_ONLY" -eq 0 ]] && ! command -v devlink >/dev/null 2>&1; then
    echo "Error: 'devlink' not found in PATH (try installing iproute2)" >&2
    exit 1
fi

# Make sure the mst service is running; ignore failure since it may already be up.
mst start >/dev/null 2>&1 || true

# mst status -v columns:
#   DEVICE_TYPE   MST   PCI   RDMA   NET   NUMA
# DEVICE_TYPE looks like "BlueField4(rev:0)" or "ConnectX9(rev:0)".
# PCI may be "bus:dev.fn" (e.g. d1:00.1) or already "domain:bus:dev.fn".
case "$DEVICE_FILTER" in
    BF4)  type_regex='^BlueField4' ;;
    CX9)  type_regex='^ConnectX9' ;;
    both) type_regex='^(BlueField4|ConnectX9)' ;;
esac

mst_output=$(mst status -v 2>/dev/null || true)
if [[ -z "$mst_output" ]]; then
    echo "Error: 'mst status -v' produced no output (is mst installed and are you root?)" >&2
    exit 1
fi

mapfile -t raw_pci_addrs < <(
    printf '%s\n' "$mst_output" \
        | awk -v re="$type_regex" '$0 ~ re { print $3 }' \
        | sort -u
)

if [[ ${#raw_pci_addrs[@]} -eq 0 ]]; then
    echo "No matching '${DEVICE_FILTER}' devices found in 'mst status -v'." >&2
    exit 1
fi

# Normalize "bus:dev.fn" to "domain:bus:dev.fn" using sysfs so that
# non-zero PCI domains (e.g. 000d:41:00.1) are handled correctly.
normalize_pci() {
    local addr="$1"
    if [[ "$addr" =~ ^[0-9a-fA-F]{4}: ]]; then
        echo "$addr"
        return
    fi
    local match
    match=$(compgen -G "/sys/bus/pci/devices/*:${addr}" 2>/dev/null | head -n1)
    if [[ -n "$match" ]]; then
        basename "$match"
    else
        echo "0000:${addr}"
    fi
}

declare -a pci_addrs=()
for a in "${raw_pci_addrs[@]}"; do
    pci_addrs+=("$(normalize_pci "$a")")
done

if [[ "$PRINT_ONLY" -eq 1 ]]; then
    printf '%s\n' "${pci_addrs[@]}"
    exit 0
fi

rc=0
for addr in "${pci_addrs[@]}"; do
    echo "===== devlink dev eswitch show pci/${addr} ====="
    if ! devlink dev eswitch show "pci/${addr}"; then
        echo "Warning: devlink failed for pci/${addr}" >&2
        rc=1
    fi
done

exit "$rc"
