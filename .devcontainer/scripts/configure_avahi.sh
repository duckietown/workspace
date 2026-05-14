#!/bin/bash

set -euo pipefail

# Thin wrapper around configure_avahi.awk so startup scripts can update an
# Avahi config file without duplicating parsing logic in shell.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AWK_SCRIPT="$SCRIPT_DIR/configure_avahi.awk"
MIN_ARGS=1
DEFAULT_INTERFACE="eth0"

if [[ $# -lt $MIN_ARGS ]]; then
    echo "Usage: configure_avahi.sh <path-to-avahi-daemon.conf> [interface]" >&2
    exit 2
fi

CONFIG_PATH="$1"
INTERFACE_NAME="${2:-$DEFAULT_INTERFACE}"
# Build the rewritten config first; set -e prevents a failed transform from
# touching the original file.
TEMP_FILE="$(mktemp)"

if [[ ! -f "$AWK_SCRIPT" ]]; then
    echo "Missing awk program: $AWK_SCRIPT" >&2
    exit 2
fi

cleanup() {
    rm -f "$TEMP_FILE"
}

# Always remove the temporary file, including validation or AWK failures.
trap cleanup EXIT

awk -v iface="$INTERFACE_NAME" -f "$AWK_SCRIPT" "$CONFIG_PATH" > "$TEMP_FILE"

# Preserve the target file path and permissions expected by the package.
cat "$TEMP_FILE" > "$CONFIG_PATH"
