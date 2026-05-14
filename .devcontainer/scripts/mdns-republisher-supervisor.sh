#!/bin/bash

set -euo pipefail

# Supervises the Python mDNS republisher and repairs Docker bridge neighbors
# for virtual robots whose container MACs can change across restarts.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MDNS_REPUBLISHER="$SCRIPT_DIR/mdns_republisher.py"
MDNS_REPUBLISHER_LOG="${1:-/tmp/mdns-republisher.log}"
MDNS_REPUBLISHER_WATCHDOG_LOG="${2:-/tmp/mdns-republisher-watchdog.log}"
SOURCE_INTERFACE="${3:-docker0}"
PUBLISH_INTERFACE="${4:-eth0}"
SUPERVISOR_INTERVAL_SECONDS="${MDNS_SUPERVISOR_INTERVAL_SECONDS:-2}"
NEIGHBOR_STATE_FILE="${MDNS_NEIGHBOR_STATE_FILE:-/tmp/mdns-republisher-neighbors}"

if [[ ! -f "$MDNS_REPUBLISHER" ]]; then
    echo "$(date -Is) ERROR: mDNS republisher not found at $MDNS_REPUBLISHER" >>"$MDNS_REPUBLISHER_WATCHDOG_LOG"
    exit 2
fi

REPUBLISHER_PID=""

republisher_pids() {
    # Match the exact Python command this supervisor starts so old instances can
    # be cleaned up without touching unrelated Python processes.
    ps -eo pid=,args= 2>/dev/null | awk -v python="/usr/bin/python3" -v script="$MDNS_REPUBLISHER" '
        $2 == python && $3 == script {print $1}
    '
}

stop_pid() {
    local pid
    pid="$1"
    [[ -n "$pid" ]] || return 0

    if ! kill -0 "$pid" >/dev/null 2>&1; then
        return 0
    fi

    kill "$pid" >/dev/null 2>&1 || true
    # Give the republisher a chance to free Avahi entry groups before SIGKILL.
    for _ in 1 2 3 4 5; do
        if ! kill -0 "$pid" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    kill -9 "$pid" >/dev/null 2>&1 || true
}

cleanup_republisher() {
    if [[ -n "$REPUBLISHER_PID" ]]; then
        stop_pid "$REPUBLISHER_PID"
        REPUBLISHER_PID=""
    fi
}

cleanup_stale_republishers() {
    local pid

    # Keep one active republisher after dev container startup reruns this script.
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        if [[ -n "$REPUBLISHER_PID" && "$pid" == "$REPUBLISHER_PID" ]]; then
            continue
        fi
        echo "$(date -Is) INFO: stopping stale mDNS republisher pid $pid" >>"$MDNS_REPUBLISHER_WATCHDOG_LOG"
        stop_pid "$pid"
    done < <(republisher_pids)
}

shutdown_supervisor() {
    cleanup_republisher
    exit 0
}

trap cleanup_republisher EXIT
trap shutdown_supervisor TERM INT HUP

interface_exists() {
    local interface_name
    interface_name="$1"
    ip -o link show "$interface_name" >/dev/null 2>&1
}

virtual_robot_names() {
    container_names | grep '^dts-virtual-' || true
}

container_names() {
    docker ps --format '{{.Names}}' 2>/dev/null || true
}

container_bridge_entry() {
    local container_name
    container_name="$1"
    # The default Docker bridge entry carries both values needed to repair the
    # host neighbor table after a virtual robot container is recreated.
    docker inspect \
        -f '{{with index .NetworkSettings.Networks "bridge"}}{{.IPAddress}} {{.MacAddress}}{{end}}' \
        "$container_name" 2>/dev/null || true
}

bridge_neighbor_mac() {
    local container_ip
    container_ip="$1"
    # Read the MAC currently known to the host bridge, if any.
    ip neigh show "$container_ip" dev docker0 2>/dev/null \
        | awk -v ip="$container_ip" '$1 == ip && $2 == "lladdr" {print $3; exit}'
}

write_virtual_robot_bridge_entries() {
    local container_name bridge_entry container_ip container_mac

    # Snapshot the active virtual robots so cleanup can compare against a stable
    # view even if Docker state changes during the loop.
    while read -r container_name; do
        [[ -n "$container_name" ]] || continue
        bridge_entry="$(container_bridge_entry "$container_name")"
        [[ -n "$bridge_entry" ]] || continue

        read -r container_ip container_mac <<<"$bridge_entry"
        [[ -n "$container_ip" && -n "$container_mac" ]] || continue

        printf '%s %s %s\n' "$container_ip" "$container_mac" "$container_name"
    done < <(virtual_robot_names)
}

cleanup_unmanaged_permanent_bridge_neighbors() {
    local active_state_file neighbor_ip neighbor_mac
    active_state_file="$1"

    # Remove permanent docker0 entries this script no longer recognizes. Leaving
    # stale permanent entries can route published robot ports to the wrong MAC.
    while read -r neighbor_ip _ neighbor_mac _; do
        [[ -n "$neighbor_ip" && -n "$neighbor_mac" ]] || continue

        if awk -v ip="$neighbor_ip" -v mac="$neighbor_mac" \
            '$1 == ip && $2 == mac {found=1} END {exit !found}' "$active_state_file"; then
            continue
        fi

        echo "$(date -Is) INFO: removing unmanaged permanent docker0 neighbor ($neighbor_ip $neighbor_mac)" \
            >>"$MDNS_REPUBLISHER_WATCHDOG_LOG"
        sudo ip neigh del "$neighbor_ip" dev docker0 >/dev/null 2>&1 || true
    done < <(ip -4 neigh show dev docker0 nud permanent 2>/dev/null || true)
}

cleanup_stale_tracked_bridge_neighbors() {
    local active_state_file tracked_ip tracked_mac tracked_name current_mac
    active_state_file="$1"

    [[ -f "$NEIGHBOR_STATE_FILE" ]] || return 0

    # Only delete entries we previously wrote, and only when the bridge still
    # points at the same stale MAC. That avoids racing Docker-owned updates.
    while read -r tracked_ip tracked_mac tracked_name; do
        [[ -n "$tracked_ip" && -n "$tracked_mac" ]] || continue

        if awk -v ip="$tracked_ip" -v mac="$tracked_mac" \
            '$1 == ip && $2 == mac {found=1} END {exit !found}' "$active_state_file"; then
            continue
        fi

        current_mac="$(bridge_neighbor_mac "$tracked_ip")"
        if [[ "$current_mac" != "$tracked_mac" ]]; then
            continue
        fi

        echo "$(date -Is) INFO: removing stale docker0 neighbor for $tracked_name ($tracked_ip $tracked_mac)" \
            >>"$MDNS_REPUBLISHER_WATCHDOG_LOG"
        sudo ip neigh del "$tracked_ip" dev docker0 >/dev/null 2>&1 || true
    done <"$NEIGHBOR_STATE_FILE"
}

sync_container_bridge_neighbors() {
    local active_state_file container_ip container_mac container_name current_mac

    # Use a temporary active-state file so cleanup and persistence see the same
    # robot list for this supervisor interval.
    active_state_file="$(mktemp)"
    write_virtual_robot_bridge_entries >"$active_state_file"

    cleanup_unmanaged_permanent_bridge_neighbors "$active_state_file"
    cleanup_stale_tracked_bridge_neighbors "$active_state_file"

    while read -r container_ip container_mac container_name; do
        [[ -n "$container_ip" && -n "$container_mac" ]] || continue

        current_mac="$(bridge_neighbor_mac "$container_ip")"
        if [[ "$current_mac" == "$container_mac" ]]; then
            continue
        fi

        # Docker's published ports and direct bridge-IP access both depend on
        # the host bridge neighbor entry pointing at the current container MAC.
        echo "$(date -Is) INFO: repairing docker0 neighbor for $container_name ($container_ip $current_mac -> $container_mac)" \
            >>"$MDNS_REPUBLISHER_WATCHDOG_LOG"
        sudo ip neigh replace "$container_ip" lladdr "$container_mac" dev docker0 nud permanent \
            >/dev/null 2>&1 || true
    done <"$active_state_file"

    mv "$active_state_file" "$NEIGHBOR_STATE_FILE"
}

resolve_source_interface() {
    local bridge_interface
    # Prefer the requested bridge, then docker0, then the first Docker-created
    # bridge name. The final fallback lets the Python process log a useful error.
    if interface_exists "$SOURCE_INTERFACE"; then
        printf '%s\n' "$SOURCE_INTERFACE"
        return 0
    fi
    if interface_exists docker0; then
        printf '%s\n' docker0
        return 0
    fi
    bridge_interface="$(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep '^br-' | head -n 1 || true)"
    printf '%s\n' "$bridge_interface"
}

resolve_publish_interface() {
    local default_interface
    # Prefer the interface requested by startup, then the kernel default route.
    if interface_exists "$PUBLISH_INTERFACE"; then
        printf '%s\n' "$PUBLISH_INTERFACE"
        return 0
    fi
    default_interface="$(ip -4 route show default 2>/dev/null | awk 'NR==1 {print $5}')"
    if [[ -n "$default_interface" ]]; then
        printf '%s\n' "$default_interface"
        return 0
    fi
    printf '%s\n' eth0
}

start_republisher() {
    local source_interface publish_interface
    source_interface="$(resolve_source_interface)"
    publish_interface="$(resolve_publish_interface)"
    [[ -n "$source_interface" ]] || source_interface="docker0"
    [[ -n "$publish_interface" ]] || publish_interface="eth0"

    # Pass resolved interfaces through the environment to keep the Python CLI
    # surface small and compatible with nohup supervision.
    MDNS_SOURCE_INTERFACE="$source_interface" \
    MDNS_PUBLISH_INTERFACE="$publish_interface" \
        /usr/bin/python3 "$MDNS_REPUBLISHER" >>"$MDNS_REPUBLISHER_LOG" 2>&1 &
    REPUBLISHER_PID=$!
    echo "$(date -Is) INFO: started mDNS republisher pid $REPUBLISHER_PID (source=$source_interface publish=$publish_interface)" \
        >>"$MDNS_REPUBLISHER_WATCHDOG_LOG"
}

while true; do
    # Docker and Avahi can come up at different speeds, so each interval both
    # repairs bridge state and restarts the republisher if needed.
    cleanup_stale_republishers

    if docker info >/dev/null 2>&1; then
        sync_container_bridge_neighbors
    fi

    if [[ -n "$REPUBLISHER_PID" ]] && ! kill -0 "$REPUBLISHER_PID" >/dev/null 2>&1; then
        echo "$(date -Is) WARN: mDNS republisher pid $REPUBLISHER_PID exited" >>"$MDNS_REPUBLISHER_WATCHDOG_LOG"
        REPUBLISHER_PID=""
    fi

    if [[ -z "$REPUBLISHER_PID" ]]; then
        start_republisher
    fi

    sleep "$SUPERVISOR_INTERVAL_SECONDS"
done
