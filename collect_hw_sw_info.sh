#!/bin/bash
#
# collect_hw_sw_info.sh
#
# Collects hardware and software information from the local host and writes a
# human-readable report. Each command that is executed is echoed to the log
# (prefixed with "$ ") immediately before its output, so the log shows both
# what was run and what the system replied.
#
# Information collected:
#   - OS / kernel / distro
#   - MFT (Mellanox Firmware Tools) version
#   - DOCA version (from /opt/mellanox/doca/VERSION)
#   - Firmware version for every Mellanox device on the PCI bus, with a
#     summary section that highlights BlueField-4 (BF4) and ConnectX-9 (CX9).
#
# Usage:
#   sudo ./collect_hw_sw_info.sh [-o /path/to/output.log]
#
# Notes:
#   - Some commands (mst, mlxfwmanager, flint, mlxconfig) require root.
#   - Missing tools are reported but do not abort the script; the script
#     continues so that as much info as possible is still captured.

set -uo pipefail

# --- Argument parsing --------------------------------------------------------
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
HOSTNAME_SHORT=$(hostname -s 2>/dev/null || hostname)
LOG_FILE="/tmp/hw_sw_info_${HOSTNAME_SHORT}_${TIMESTAMP}.log"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)
            LOG_FILE="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,22p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown parameter: $1" >&2
            exit 1
            ;;
    esac
done

# Send every line of stdout/stderr from this script to both the terminal and
# the log file. From here on, plain `echo` already writes to the log.
exec > >(tee -a "$LOG_FILE") 2>&1

# --- Helpers -----------------------------------------------------------------

# Print a section header so the log is easy to scan.
section() {
    echo
    echo "============================================================"
    echo " $*"
    echo "============================================================"
}

# Echo the command, then run it. stderr is merged into stdout so the log
# captures any diagnostic output the tool emits.
#
# Usage: run_cmd "<command line as a single string>"
run_cmd() {
    local cmd="$1"
    echo
    echo "\$ ${cmd}"
    # Use bash -c so that pipes/redirects inside the command string work.
    bash -c "${cmd}" 2>&1 || echo "[exit=$?]"
}

# Returns 0 if the binary is on $PATH, 1 otherwise. Logs the check.
have_cmd() {
    if command -v "$1" >/dev/null 2>&1; then
        return 0
    fi
    echo "[skip] '$1' not found in PATH"
    return 1
}

# --- Header ------------------------------------------------------------------

section "Report metadata"
echo "Generated at : $(date)"
echo "Hostname     : $(hostname)"
echo "Log file     : ${LOG_FILE}"
echo "User         : $(id -un) (uid=$(id -u))"
if [[ "$(id -u)" -ne 0 ]]; then
    echo "WARNING: not running as root; some firmware/MFT queries may fail."
fi

# --- OS / kernel -------------------------------------------------------------

section "OS / kernel"

run_cmd "uname -a"
run_cmd "uname -r"

if [[ -f /etc/os-release ]]; then
    run_cmd "cat /etc/os-release"
else
    echo "[skip] /etc/os-release not present"
fi

if have_cmd lsb_release; then
    run_cmd "lsb_release -a"
fi

if [[ -f /etc/redhat-release ]]; then
    run_cmd "cat /etc/redhat-release"
fi

# --- MFT ---------------------------------------------------------------------

section "MFT (Mellanox Firmware Tools)"

# We capture `mst status -v` once into MST_STATUS_OUT so we can both display
# it here and parse it later (to pick only BF4/CX9 devices for `flint q`).
MST_STATUS_OUT=""
if have_cmd mst; then
    run_cmd "mst version"
    # Make sure the MST device nodes exist; needed by mlxfwmanager / flint.
    run_cmd "mst start"

    echo
    echo "\$ mst status -v"
    MST_STATUS_OUT=$(mst status -v 2>&1 || true)
    echo "$MST_STATUS_OUT"
fi

if have_cmd mlxconfig; then
    run_cmd "mlxconfig --version"
fi

if have_cmd flint; then
    run_cmd "flint --version"
fi

# --- DOCA --------------------------------------------------------------------

section "DOCA"

DOCA_VERSION_FILE="/opt/mellanox/doca/VERSION"
if [[ -f "${DOCA_VERSION_FILE}" ]]; then
    run_cmd "cat ${DOCA_VERSION_FILE}"
else
    echo "[skip] ${DOCA_VERSION_FILE} not present (DOCA may not be installed)"
fi

# Some DOCA installs also ship an info script; record it if available.
if [[ -x /opt/mellanox/doca/tools/doca_info ]]; then
    run_cmd "/opt/mellanox/doca/tools/doca_info"
fi

# OFED / DOCA-OFED bundle version is useful context, log it if available.
if have_cmd ofed_info; then
    run_cmd "ofed_info -s"
fi

# --- PCI inventory -----------------------------------------------------------

section "Mellanox PCI inventory"

run_cmd "lspci -nn | grep -iE 'Mellanox|BlueField|ConnectX'"

# --- Firmware versions -------------------------------------------------------

section "Firmware versions (all Mellanox devices)"

if have_cmd mlxfwmanager; then
    run_cmd "mlxfwmanager --query"
fi

# Per-device flint query — produces one tidy block per device (FW Version,
# Product Version, PSID, Device ID, etc.) which is easy to grep later.
#
# We restrict to BlueField4 / ConnectX9 devices only. Hosts in this fleet
# also expose Vera (CPU) and GR100 (GPU) entries via MST; running flint on
# those produces "MFE_UNSUPPORTED_DEVICE" / "GPUs are not supported" noise.
#
# We also strip `.N` sub-function suffixes (e.g. `mt4133_pciconf0.1` → 
# `mt4133_pciconf0`) so each physical chip is queried only once.
if have_cmd flint && have_cmd mst; then
    mapfile -t NIC_DEVS < <(echo "$MST_STATUS_OUT" \
        | awk '/^BlueField[0-9]+\(/ || /^ConnectX[0-9]+\(/ {print $2}' \
        | sed -E 's/\.[0-9]+$//' \
        | sort -u)

    if [[ ${#NIC_DEVS[@]} -eq 0 ]]; then
        echo "[skip] no BlueField/ConnectX MST devices found"
    else
        echo "Querying ${#NIC_DEVS[@]} NIC chip(s): ${NIC_DEVS[*]}"
        for dev in "${NIC_DEVS[@]}"; do
            section "flint query: ${dev}"
            run_cmd "flint -d ${dev} q"
        done
    fi
fi

# --- BF4 / CX9 summary -------------------------------------------------------
#
# Re-run mlxfwmanager once and pull out only the BlueField-4 and ConnectX-9
# entries so the user has a one-glance answer to "what FW are BF4 and CX9
# running?". If the host has neither, we say so explicitly.
#
# Note: mlxfwmanager prints `Device Type:      BlueField4` (no hyphen) and
# `Device Type:      ConnectX9` (no hyphen), so we match BlueField-?4 and
# ConnectX-?9 to be tolerant of either form.

section "Summary: BlueField-4 (BF4) and ConnectX-9 (CX9) firmware"

if have_cmd mlxfwmanager; then
    MLX_OUT=$(mlxfwmanager --query 2>&1 || true)

    # Walk mlxfwmanager output as a series of "Device #N:" blocks and print
    # one short line per BF4 / CX9 device with FW version + PCI device.
    echo "$MLX_OUT" | awk '
        function flush() {
            if (kind != "" && fw != "" && pci != "")
                printf("%-12s  FW %-12s  %s\n", kind, fw, pci)
            kind = ""; fw = ""; pci = ""
        }
        /^Device #[0-9]+:/ { flush(); next }
        /Device Type:[[:space:]]*BlueField-?4/ { kind = "BlueField-4" }
        /Device Type:[[:space:]]*ConnectX-?9/  { kind = "ConnectX-9"  }
        /PCI Device Name:/ {
            sub(/^.*PCI Device Name:[[:space:]]*/, "", $0); pci = $0
        }
        /^[[:space:]]+FW[[:space:]]+[0-9]/ { fw = $2 }
        END { flush() }
    '

    BF4_COUNT=$(echo "$MLX_OUT" | grep -cE "Device Type:[[:space:]]*BlueField-?4" || true)
    CX9_COUNT=$(echo "$MLX_OUT" | grep -cE "Device Type:[[:space:]]*ConnectX-?9"  || true)
    echo
    echo "BlueField-4 devices found: ${BF4_COUNT}"
    echo "ConnectX-9  devices found: ${CX9_COUNT}"
    if [[ "${BF4_COUNT}" -eq 0 && "${CX9_COUNT}" -eq 0 ]]; then
        echo "NOTE: no BF4 or CX9 devices were detected on this host."
    fi
else
    echo "[skip] mlxfwmanager not installed; cannot build BF4/CX9 summary."
fi

section "Done"
echo "Full log written to: ${LOG_FILE}"

