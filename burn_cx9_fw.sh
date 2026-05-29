#!/usr/bin/env bash
#
# burn_cx9_fw.sh -- Burn firmware to every ConnectX-9 (CX9) device in the host.
#
# Usage:
#   sudo ./burn_cx9_fw.sh <firmware.bin> [--yes] [--reset]
#
#   <firmware.bin>  Path to the .bin firmware image to burn (required).
#   --yes           Skip the interactive confirmation prompt.
#   --reset         Run `mlxfwreset` after burning so the new firmware is loaded
#                   without a host power cycle. Default: do NOT reset; the new
#                   image takes effect after the next system power cycle.
#
# Requirements: mstflint package (mst, flint, and mlxfwreset if --reset is used)
#               and root privileges.

set -u
set -o pipefail

# ---------- helpers ---------------------------------------------------------

log()  { printf '[%(%H:%M:%S)T] %s\n' -1 "$*"; }
warn() { printf '[%(%H:%M:%S)T] WARN: %s\n' -1 "$*" >&2; }
die()  { printf '[%(%H:%M:%S)T] ERROR: %s\n' -1 "$*" >&2; exit 1; }

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Required tool '$1' not found in PATH. Install mstflint."
}

# ---------- argument parsing -----------------------------------------------

FW_BIN=""
ASSUME_YES=0
DO_RESET=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)     usage ;;
        --yes|-y)      ASSUME_YES=1 ;;
        --reset)       DO_RESET=1 ;;
        -*)            die "Unknown option: $1" ;;
        *)
            if [[ -z "$FW_BIN" ]]; then
                FW_BIN="$1"
            else
                die "Unexpected extra argument: $1"
            fi
            ;;
    esac
    shift
done

[[ -n "$FW_BIN" ]]   || usage
[[ -r "$FW_BIN" ]]   || die "Firmware file not readable: $FW_BIN"
[[ -s "$FW_BIN" ]]   || die "Firmware file is empty: $FW_BIN"
[[ "$EUID" -eq 0 ]]  || die "Must be run as root (try: sudo $0 ...)"

require_cmd mst
require_cmd flint
(( DO_RESET )) && require_cmd mlxfwreset

FW_BIN="$(readlink -f "$FW_BIN")"
log "Firmware image: $FW_BIN"

# ---------- start MST and discover CX9 devices -----------------------------

log "Starting MST service..."
mst start >/dev/null 2>&1 || warn "mst start returned non-zero (already running?)"

# `mst status -v` produces a table whose first column is the device family
# (e.g. "ConnectX9(rev:0)" or "ConnectX-9") and the second column is the MST
# path. Multi-PF cards expose one base device plus per-PF aliases:
#   /dev/mst/mt4131_pciconf0       <-- physical card (burn target)
#   /dev/mst/mt4131_pciconf0.1     <-- PF1 alias, same card
#   /dev/mst/mt4131_pciconf0.2     <-- PF2 alias, same card
# Firmware lives on the card, not per-PF, so keep only entries whose MST path
# ends with "pciconf<N>" (no trailing ".<M>") to get one entry per physical
# CX9 device.
mapfile -t CX9_DEVS < <(
    mst status -v 2>/dev/null \
      | awk 'tolower($1) ~ /^connectx-?9/ && $2 ~ /pciconf[0-9]+$/ { print $2 }' \
      | sort -u
)

if [[ ${#CX9_DEVS[@]} -eq 0 ]]; then
    die "No ConnectX-9 devices found via 'mst status -v'."
fi

log "Discovered ${#CX9_DEVS[@]} CX9 device(s):"
for d in "${CX9_DEVS[@]}"; do
    # Show PSID + current FW version for each device for a sanity check.
    psid=$(flint -d "$d" query 2>/dev/null | awk -F': *' '/^PSID/    {print $2; exit}')
    fwver=$(flint -d "$d" query 2>/dev/null | awk -F': *' '/^FW Version/{print $2; exit}')
    log "  - $d   PSID=${psid:-?}   FW=${fwver:-?}"
done

# ---------- confirm --------------------------------------------------------

if (( ! ASSUME_YES )); then
    read -r -p "Burn '$FW_BIN' to ALL ${#CX9_DEVS[@]} CX9 device(s)? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || die "Aborted by user."
fi

# ---------- burn loop ------------------------------------------------------

declare -a OK_DEVS=()
declare -a FAIL_DEVS=()

for dev in "${CX9_DEVS[@]}"; do
    log "=== Burning $dev ==="
    if flint -d "$dev" -i "$FW_BIN" -y burn; then
        log "Burn OK on $dev"
        OK_DEVS+=("$dev")
    else
        warn "Burn FAILED on $dev"
        FAIL_DEVS+=("$dev")
    fi
done

# ---------- optional reset to load new FW ----------------------------------

if (( DO_RESET )) && [[ ${#OK_DEVS[@]} -gt 0 ]]; then
    for dev in "${OK_DEVS[@]}"; do
        log "Resetting $dev to load new firmware..."
        if ! mlxfwreset -d "$dev" -y -l 3 reset; then
            warn "mlxfwreset failed on $dev -- a host power cycle will be required."
        fi
    done
elif [[ ${#OK_DEVS[@]} -gt 0 ]]; then
    log "Skipping device reset (default). Power cycle the system to load the new firmware."
fi

# ---------- summary --------------------------------------------------------

log "----- Summary -----"
log "Succeeded: ${#OK_DEVS[@]}"
for d in "${OK_DEVS[@]}";   do log "  OK   $d"; done
log "Failed:    ${#FAIL_DEVS[@]}"
for d in "${FAIL_DEVS[@]}"; do log "  FAIL $d"; done

[[ ${#FAIL_DEVS[@]} -eq 0 ]] || exit 2
exit 0
