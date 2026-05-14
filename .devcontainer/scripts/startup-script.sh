#!/bin/bash

set -e

# Dev container startup orchestration: bring up the services that the base image
# cannot safely start during build, then keep virtual robot discovery visible.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AVAHI_CONF="/etc/avahi/avahi-daemon.conf"
AVAHI_CONFIGURER="$SCRIPT_DIR/configure_avahi.sh"
MDNS_REPUBLISHER="$SCRIPT_DIR/mdns_republisher.py"
MDNS_REPUBLISHER_SUPERVISOR="$SCRIPT_DIR/mdns-republisher-supervisor.sh"
MDNS_REPUBLISHER_LOG="/tmp/mdns-republisher.log"
MDNS_REPUBLISHER_WATCHDOG_LOG="/tmp/mdns-republisher-watchdog.log"
MDNS_REPUBLISHER_SUPERVISOR_LOG="/tmp/mdns-republisher-supervisor.log"
DOCKER_STATUS="not checked"
DBUS_STATUS="not checked"
AVAHI_STATUS="not checked"
MDNS_STATUS="not checked"
VNC_STATUS="not checked"
NOVNC_STATUS="not checked"

interface_exists() {
    local iface="$1"
    # ip returns non-zero for missing interfaces without needing text parsing.
    ip -o link show "$iface" >/dev/null 2>&1
}

detect_publish_interface() {
    local iface
    # Prefer the default route because that is the interface visible outside the
    # nested Docker network.
    iface="$(ip -4 route show default 2>/dev/null | awk 'NR==1 {print $5}')"
    if [ -z "$iface" ]; then
        iface="$(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -E '^(eth|en|wlan)' | head -n 1)"
    fi
    if [ -z "$iface" ]; then
        iface="eth0"
    fi
    echo "$iface"
}

detect_source_interface() {
    # docker0 is the normal bridge for virtual Duckietown robot containers.
    if interface_exists "docker0"; then
        echo "docker0"
        return
    fi

    local iface
    iface="$(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep '^br-' | head -n 1)"
    if [ -n "$iface" ]; then
        echo "$iface"
        return
    fi

    echo "docker0"
}

wait_for_docker_ready() {
    local max_attempts=30
    local attempt=1
    # docker-in-docker can still be initializing after its startup script exits.
    while [ "$attempt" -le "$max_attempts" ]; do
        if docker info >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done
    return 1
}

port_listening() {
    local port="$1"
    ss -H -ltn 2>/dev/null | awk -v suffix=":${port}" '
        $4 ~ suffix "$" { found = 1 }
        END { exit !found }
    '
}

describe_caroot() {
    if [ -z "${CAROOT:-}" ]; then
        echo "not configured"
        return
    fi

    if [ ! -d "$CAROOT" ]; then
        echo "missing at $CAROOT"
        return
    fi

    if [ -f "$CAROOT/rootCA.pem" ] && [ -f "$CAROOT/rootCA-key.pem" ]; then
        echo "ready at $CAROOT"
        return
    fi

    echo "mounted at $CAROOT, but mkcert CA files were not found"
}

print_workspace_health_summary() {
    local caroot_status
    caroot_status="$(describe_caroot)"

    cat <<EOF

Duckietown workspace startup summary
  Docker: ${DOCKER_STATUS}
  D-Bus: ${DBUS_STATUS}
  Avahi: ${AVAHI_STATUS}
  Virtual robot discovery: ${MDNS_STATUS}
  VNC desktop: ${VNC_STATUS}
  noVNC browser desktop: ${NOVNC_STATUS}
  mkcert CA: ${caroot_status}

Useful endpoints and logs
  noVNC: http://localhost:6080
  mDNS republisher: ${MDNS_REPUBLISHER_LOG}
  mDNS watchdog: ${MDNS_REPUBLISHER_WATCHDOG_LOG}
  noVNC log: /tmp/novnc.log
  VNC log: /tmp/xtigervnc.log
EOF
}

stop_existing_mdns_supervisors() {
    # VS Code may rerun the startup script in an existing container; restart the
    # watchdog so it picks up the latest script and interface values.
    if pgrep -f 'mdns-republisher-supervisor.sh' >/dev/null 2>&1; then
        echo "Restarting Duckietown mDNS republisher supervisor..."
        pkill -f 'mdns-republisher-supervisor.sh' >/dev/null 2>&1 || true
        sleep 1
    fi
}

repair_git_lfs_config() {
    # Dev container tooling can copy host Git LFS filter paths into the container.
    # Reinstall filters only when the configured absolute executable is invalid.
    if ! command -v git >/dev/null 2>&1 || ! command -v git-lfs >/dev/null 2>&1; then
        return
    fi

    local bad_lfs_executable
    bad_lfs_executable="$(git config --global --get-regexp '^filter\.lfs\.' 2>/dev/null | awk '
        {
            split($2, parts, " ")
            executable = parts[1]
            if (executable ~ /^\//) {
                print executable
                exit
            }
        }
    ')"

    if [ -n "$bad_lfs_executable" ] && [ ! -x "$bad_lfs_executable" ]; then
        echo "Repairing Git LFS filter config for this container..."
        git lfs install --force >/dev/null
    fi
}

# Configure DNS resolver options
# The short timeout and single-request-reopen setting reduce resolver stalls in
# nested Docker networking during dev container startup.
sudo grep -q "single-request-reopen" /etc/resolv.conf || echo "options timeout:1 attempts:1 single-request-reopen" | sudo tee -a /etc/resolv.conf > /dev/null

# Start D-Bus daemon (required for Avahi)
if ! pgrep -x dbus-daemon > /dev/null; then
    echo "Starting D-Bus daemon..."
    sudo mkdir -p /var/run/dbus
    # Remove stale PID file if it exists
    sudo rm -f /run/dbus/pid
    sudo dbus-daemon --system --fork
fi
if pgrep -x dbus-daemon > /dev/null; then
    DBUS_STATUS="running"
else
    DBUS_STATUS="not detected"
fi

# Fix Docker credential store issue
# Keep Docker CLI auth config minimal inside the container so host credential
# helpers are not referenced from an environment where they do not exist.
mkdir -p ~/.docker
echo "{}" > ~/.docker/config.json

# Repair host-specific Git LFS paths copied into the container by the dev container tooling.
repair_git_lfs_config

# Call docker-in-docker startup script
if [ -f "/usr/local/share/docker-init.sh" ]; then
    if docker info >/dev/null 2>&1; then
        echo "Docker already healthy; skipping docker-in-docker re-initialization"
    else
        echo "Starting Docker via docker-in-docker feature..."
        /usr/local/share/docker-init.sh 2>&1 | grep -v -E "(echo: write error|Device or resource busy|Failed to enable nesting, retrying)" || true
    fi
fi
if docker info >/dev/null 2>&1; then
    DOCKER_STATUS="ready"
else
    DOCKER_STATUS="not ready yet"
fi

# Configure and start Avahi daemon
SOURCE_INTERFACE="$(detect_source_interface)"
PUBLISH_INTERFACE="$(detect_publish_interface)"
echo "Configuring Avahi for ${SOURCE_INTERFACE} to ${PUBLISH_INTERFACE} republishing..."
# Bind Avahi to the host-visible interface; the Python republisher handles the
# selected Docker bridge separately.
sudo bash "$AVAHI_CONFIGURER" "$AVAHI_CONF" "$PUBLISH_INTERFACE"
sudo avahi-daemon -k >/dev/null 2>&1 || true
sudo pkill -x avahi-daemon >/dev/null 2>&1 || true
sudo rm -f /run/avahi-daemon/pid
echo "Starting Avahi daemon..."
sudo avahi-daemon -D
if pgrep -x avahi-daemon > /dev/null; then
    AVAHI_STATUS="running on ${PUBLISH_INTERFACE}"
else
    AVAHI_STATUS="not detected"
fi

# Republish virtual robot mDNS records on the dev container interface that
# the host can see. The records point to the dev container IP; Docker-published
# robot ports provide the actual path to services such as DTPS.
if [ ! -f "$MDNS_REPUBLISHER" ]; then
    echo "WARNING: mDNS republisher not found at $MDNS_REPUBLISHER"
    MDNS_STATUS="republisher script missing"
elif [ ! -f "$MDNS_REPUBLISHER_SUPERVISOR" ]; then
    echo "WARNING: mDNS republisher supervisor not found at $MDNS_REPUBLISHER_SUPERVISOR"
    MDNS_STATUS="supervisor script missing"
else
    # Docker can be slow to come up after system/container restarts.
    # Do not fail startup if it is not immediately ready.
    if wait_for_docker_ready; then
        DOCKER_STATUS="ready"
    else
        echo "WARNING: Docker not ready during startup; republisher watchdog will retry automatically"
        DOCKER_STATUS="not ready yet; mDNS watchdog will retry"
    fi

    chmod +x "$MDNS_REPUBLISHER_SUPERVISOR" >/dev/null 2>&1 || true

    stop_existing_mdns_supervisors

    echo "Starting Duckietown mDNS republisher..."
    nohup "$MDNS_REPUBLISHER_SUPERVISOR" \
        "$MDNS_REPUBLISHER_LOG" \
        "$MDNS_REPUBLISHER_WATCHDOG_LOG" \
        "$SOURCE_INTERFACE" \
        "$PUBLISH_INTERFACE" \
        >>"$MDNS_REPUBLISHER_SUPERVISOR_LOG" 2>&1 </dev/null &
    disown

    # Best-effort startup check so failures are visible quickly.
    for _ in $(seq 1 10); do
        if pgrep -f "/usr/bin/python3 $MDNS_REPUBLISHER" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if ! pgrep -f "/usr/bin/python3 $MDNS_REPUBLISHER" >/dev/null 2>&1; then
        echo "WARNING: mDNS republisher process not detected after startup"
        MDNS_STATUS="not detected; see ${MDNS_REPUBLISHER_WATCHDOG_LOG}"
    else
        MDNS_STATUS="running"
    fi
fi

# Call desktop-lite startup script
if [ -f "/usr/local/share/desktop-init.sh" ] && [ ! -f "/tmp/desktop-init.lock" ]; then
    echo "Starting desktop via desktop-lite feature..."
    # Use a lock file because this startup script can be invoked repeatedly.
    touch /tmp/desktop-init.lock
    
    # Clean any stale sockets
    sudo rm -f /tmp/.X11-unix/X99 /tmp/.X99-lock 2>/dev/null
    mkdir -p $HOME/.vnc
    
    # Initialize Firefox profile directory
    mkdir -p $HOME/.mozilla/firefox
    
    # Start X server with VNC using tigervncserver (includes window manager)
    setsid tigervncserver :99 -SecurityTypes None -localhost >/tmp/xtigervnc.log 2>&1 </dev/null &
    disown
    
    echo "Waiting for VNC to start..."
    # Wait for VNC to start (up to 30 seconds)
    for i in $(seq 1 30); do
        if port_listening 5999; then
            break
        fi
        sleep 1
    done
    if port_listening 5999; then
        echo "VNC started on port 5999"
        VNC_STATUS="running on port 5999"
    else
        echo "ERROR: VNC failed to start. Check /tmp/xtigervnc.log"
        VNC_STATUS="not detected; see /tmp/xtigervnc.log"
    fi
    
    # Start noVNC if VNC is running
    if port_listening 5999; then
        if [ -d "/usr/local/novnc" ]; then
            # Start websockify from its directory so the module can be found
            cd /usr/local/novnc/websockify-0.10.0
            setsid python3 -m websockify --web /usr/local/novnc/noVNC-1.3.0 6080 localhost:5999 >/tmp/novnc.log 2>&1 </dev/null &
            echo $! > /tmp/novnc.pid
            disown
            cd - >/dev/null
            
            # Wait for noVNC to start (up to 30 seconds)
            for i in $(seq 1 30); do
                if port_listening 6080; then
                    break
                fi
                sleep 1
            done
            
            if port_listening 6080; then
                echo "noVNC started on port 6080"
                NOVNC_STATUS="running at http://localhost:6080"
            else
                echo "WARNING: noVNC may not have started. Check /tmp/novnc.log"
                NOVNC_STATUS="not detected; see /tmp/novnc.log"
            fi
        else
            NOVNC_STATUS="noVNC installation not found"
        fi
    else
        NOVNC_STATUS="not started because VNC is unavailable"
    fi
else
    if [ ! -f "/usr/local/share/desktop-init.sh" ]; then
        VNC_STATUS="desktop-lite startup script not found"
        NOVNC_STATUS="desktop-lite startup script not found"
    else
        if port_listening 5999; then
            VNC_STATUS="already running on port 5999"
        else
            VNC_STATUS="desktop lock present; port 5999 not detected"
        fi
        if port_listening 6080; then
            NOVNC_STATUS="already running at http://localhost:6080"
        else
            NOVNC_STATUS="desktop lock present; port 6080 not detected"
        fi
    fi
fi

print_workspace_health_summary
