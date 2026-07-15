#!/bin/sh
set -eu

# Write the workspace image tag expected by Docker Compose interpolation
# based on the host architecture running this script.
devcontainer_dir=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
repo_root=$(CDPATH= cd -- "$devcontainer_dir/.." && pwd)
env_file="$devcontainer_dir/.env"
host_runner_script="$devcontainer_dir/scripts/host_runner.py"
host_runner_bootstrap_script="$devcontainer_dir/scripts/host_runner_bootstrap.py"
host_runner_runtime_dir="/tmp/duckietown"
repo_local_host_runner_pid_file="$devcontainer_dir/.host_runner.pid"
repo_local_host_runner_port_file="$devcontainer_dir/.host_runner.port"
repo_local_host_runner_version_file="$devcontainer_dir/.host_runner.version"
repo_local_host_runner_log_file="$devcontainer_dir/.host_runner.log"
repo_local_host_runner_token_file="$devcontainer_dir/.host_runner.token"
host_runner_pid_file="$host_runner_runtime_dir/.host_runner.pid"
host_runner_port_file="$host_runner_runtime_dir/.host_runner.port"
host_runner_version_file="$host_runner_runtime_dir/.host_runner.version"
host_runner_log_file="$host_runner_runtime_dir/.host_runner.log"
host_runner_token_file="$host_runner_runtime_dir/.host_runner.token"
host_runner_request_dir="$host_runner_runtime_dir/host_runner_requests"
host_runner_shared_env_file="$host_runner_request_dir/host_runner_endpoint.env"
host_runner_default_port=59321
host_runner_port="$host_runner_default_port"
host_runner_container_root="/home/ubuntu"
host_runner_engine_host="127.0.0.1"

select_workspace_image_tag() {
  # Docker image manifests use explicit Duckietown architecture suffixes.
  case "$(uname -m)" in
    x86_64|amd64)
      printf '%s\n' "latest-amd64"
      ;;
    arm64|aarch64)
      printf '%s\n' "latest-arm64v8"
      ;;
    *)
      printf '%s\n' "Unsupported host architecture: $(uname -m). Expected amd64/x86_64 or arm64/aarch64." >&2
      exit 1
      ;;
  esac
}

running_inside_container() {
  [ -f "/.dockerenv" ] || [ -f "/run/.containerenv" ]
}

detect_host_runner_root() {
  parent_root=$(CDPATH= cd -- "$repo_root/.." && pwd)
  printf '%s\n' "$parent_root"
}

ensure_host_runner_runtime_dir() {
  mkdir -p "$host_runner_runtime_dir"
}

migrate_repo_local_host_runner_artifact() {
  source_path=$1
  runtime_path=$2

  if [ -e "$runtime_path" ]; then
    rm -f "$source_path"
    return
  fi
  if [ -e "$source_path" ]; then
    mv "$source_path" "$runtime_path"
  fi
}

migrate_repo_local_host_runner_artifacts() {
  ensure_host_runner_runtime_dir
  migrate_repo_local_host_runner_artifact "$repo_local_host_runner_pid_file" "$host_runner_pid_file"
  migrate_repo_local_host_runner_artifact "$repo_local_host_runner_port_file" "$host_runner_port_file"
  migrate_repo_local_host_runner_artifact "$repo_local_host_runner_version_file" "$host_runner_version_file"
  migrate_repo_local_host_runner_artifact "$repo_local_host_runner_log_file" "$host_runner_log_file"
  migrate_repo_local_host_runner_artifact "$repo_local_host_runner_token_file" "$host_runner_token_file"
}

run_host_python() {
  if command -v python3 >/dev/null 2>&1; then
    python3 "$@"
    return
  fi
  if command -v python >/dev/null 2>&1; then
    python "$@"
    return
  fi
  if command -v py >/dev/null 2>&1; then
    py -3 "$@"
    return
  fi

  printf '%s\n' "Could not find a host Python interpreter required for host GUI delegation." >&2
  return 1
}

start_host_runner_supervisor() {
  nohup "$@" "$host_runner_bootstrap_script" \
    supervise \
    --script "$host_runner_script" \
    --host-root "$host_runner_root" \
    --container-root "$host_runner_container_root" \
    --requests-dir "$host_runner_request_dir" \
    --listen-host 127.0.0.1 \
    --listen-port "$host_runner_port" \
    --token "$token" \
    > "$host_runner_log_file" 2>&1 < /dev/null &
  runner_pid=$!
}

host_runner_ready() {
  ready_timeout=${1:-1}

  run_host_python \
    "$host_runner_bootstrap_script" \
    wait-ready \
    --host 127.0.0.1 \
    --port "$host_runner_port" \
    --timeout "$ready_timeout"
}

host_runner_port_is_numeric() {
  case "$1" in
    ''|*[!0-9]*)
      return 1
      ;;
  esac

  return 0
}

read_host_runner_port_from_url() {
  host_runner_url_value=$1

  parsed_host_runner_port=$(printf '%s\n' "$host_runner_url_value" | sed -n 's#^[[:alpha:]][[:alnum:]+.-]*://[^:/]*:\([0-9][0-9]*\)/.*#\1#p')
  if ! host_runner_port_is_numeric "$parsed_host_runner_port"; then
    return 1
  fi

  printf '%s\n' "$parsed_host_runner_port"
}

read_recorded_host_runner_port() {
  if [ -f "$env_file" ]; then
    recorded_host_runner_url=$(sed -n 's/^DTS_HOST_RUNNER_URL=//p' "$env_file" | head -n 1)
    if [ -n "$recorded_host_runner_url" ]; then
      recorded_host_runner_port=$(read_host_runner_port_from_url "$recorded_host_runner_url" || true)
      if host_runner_port_is_numeric "$recorded_host_runner_port"; then
        printf '%s\n' "$recorded_host_runner_port"
        return 0
      fi
    fi
  fi

  if [ -f "$host_runner_port_file" ]; then
    recorded_host_runner_port=$(tr -d '[:space:]' < "$host_runner_port_file")
    if host_runner_port_is_numeric "$recorded_host_runner_port"; then
      printf '%s\n' "$recorded_host_runner_port"
      return 0
    fi
  fi

  printf '%s\n' "$host_runner_default_port"
}

find_listening_pid_on_port() {
  listener_port=$1

  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi

  listener_pid=$(lsof -nP -iTCP:"$listener_port" -sTCP:LISTEN -Fp 2>/dev/null | awk '
    /^p/ {
      print substr($0, 2)
      exit
    }
  ')

  if [ -z "$listener_pid" ]; then
    return 1
  fi

  printf '%s\n' "$listener_pid"
}

find_listening_pid_on_host_runner_port() {
  find_listening_pid_on_port "$host_runner_port"
}

read_process_command_line() {
  process_id=$1

  command_line=$(ps -ww -p "$process_id" -o command= 2>/dev/null || true)
  if [ -n "$command_line" ]; then
    printf '%s\n' "$command_line"
    return 0
  fi

  ps -p "$process_id" -o command= 2>/dev/null || true
}

find_host_runner_listener_pid_for_script() {
  runner_script=$1
  listener_port=$2

  listener_pid=$(find_listening_pid_on_port "$listener_port" || true)
  if [ -z "$listener_pid" ] || ! command -v ps >/dev/null 2>&1; then
    return 1
  fi

  listener_command=$(read_process_command_line "$listener_pid")
  if printf '%s\n' "$listener_command" | grep -F "$runner_script" >/dev/null 2>&1; then
    printf '%s\n' "$listener_pid"
    return 0
  fi

  return 1
}

find_current_host_runner_listener_pid() {
  find_host_runner_listener_pid_for_script "$host_runner_script" "$host_runner_port"
}

find_managed_host_runner_listener_pid() {
  find_current_host_runner_listener_pid
}

wait_for_process_exit() {
  process_id=$1
  attempts=20

  while [ "$attempts" -gt 0 ]; do
    if ! kill -0 "$process_id" 2>/dev/null; then
      return 0
    fi
    sleep 1
    attempts=$((attempts - 1))
  done

  return 1
}

default_host_runner_url() {
  case "$(uname -s)" in
    Linux)
      printf 'http://127.0.0.1:%s/run\n' "$host_runner_port"
      ;;
    Darwin|MINGW*|MSYS*|CYGWIN*)
      printf 'http://host.docker.internal:%s/run\n' "$host_runner_port"
      ;;
    *)
      printf 'http://host.docker.internal:%s/run\n' "$host_runner_port"
      ;;
  esac
}

select_available_host_runner_port() {
  preferred_host_runner_port=$1

  run_host_python \
    "$host_runner_bootstrap_script" \
    select-port \
    --preferred "$preferred_host_runner_port"
}

choose_host_runner_port() {
  preferred_host_runner_port=$1
  selected_host_runner_port=$(select_available_host_runner_port "$preferred_host_runner_port")
  if [ "$selected_host_runner_port" = "$preferred_host_runner_port" ]; then
    printf '%s\n' "$selected_host_runner_port"
    return 0
  fi

  conflicting_listener_pid=$(find_listening_pid_on_port "$preferred_host_runner_port" || true)
  if [ -n "$conflicting_listener_pid" ]; then
    conflicting_listener_command=$(read_process_command_line "$conflicting_listener_pid")
    printf '%s\n' "Host runner port $preferred_host_runner_port is already in use by PID $conflicting_listener_pid. Selecting port $selected_host_runner_port automatically." >&2
    if [ -n "$conflicting_listener_command" ]; then
      printf '%s\n' "Port $preferred_host_runner_port command: $conflicting_listener_command" >&2
    fi
  else
    printf '%s\n' "Host runner port $preferred_host_runner_port became unavailable. Selecting port $selected_host_runner_port automatically." >&2
  fi

  printf '%s\n' "$selected_host_runner_port"
}

remove_host_runner_shared_endpoint_files() {
  rm -f "$host_runner_shared_env_file"
  rm -f "$host_runner_shared_env_file.tmp"
}

scrub_stale_host_runner_shared_endpoint() {
  expected_endpoint_url=$1
  expected_endpoint_token=$2

  run_host_python \
    "$host_runner_bootstrap_script" \
    scrub-endpoint \
    --endpoint-file "$host_runner_shared_env_file" \
    --expected-url "$expected_endpoint_url" \
    --expected-token "$expected_endpoint_token" \
    --quiet
}

write_host_runner_shared_env() {
  endpoint_url=$1
  endpoint_token=$2
  endpoint_timeout=${3:-}
  tmp_host_runner_shared_env_file="$host_runner_shared_env_file.tmp"

  mkdir -p "$host_runner_request_dir"
  : > "$tmp_host_runner_shared_env_file"
  printf 'DTS_HOST_RUNNER_URL=%s\n' "$endpoint_url" >> "$tmp_host_runner_shared_env_file"
  printf 'DTS_HOST_RUNNER_TOKEN=%s\n' "$endpoint_token" >> "$tmp_host_runner_shared_env_file"
  printf 'DTS_HOST_RUNNER_REQUESTS_DIR=%s\n' "$host_runner_request_dir" >> "$tmp_host_runner_shared_env_file"
  if [ -n "$endpoint_timeout" ]; then
    printf 'DTS_HOST_RUNNER_TIMEOUT=%s\n' "$endpoint_timeout" >> "$tmp_host_runner_shared_env_file"
  fi
  mv "$tmp_host_runner_shared_env_file" "$host_runner_shared_env_file"
}

record_host_runner_endpoint() {
  endpoint_token=$1
  endpoint_timeout=${2:-}
  endpoint_url=$(default_host_runner_url)

  printf '%s\n' "$host_runner_port" > "$host_runner_port_file"
  write_host_runner_shared_env "$endpoint_url" "$endpoint_token" "$endpoint_timeout"
}

generate_host_runner_token() {
  if [ -f "$host_runner_token_file" ] && [ -s "$host_runner_token_file" ]; then
    cat "$host_runner_token_file"
    return 0
  fi

  token=$(run_host_python "$host_runner_bootstrap_script" generate-token)

  printf '%s\n' "$token" > "$host_runner_token_file"
  printf '%s\n' "$token"
}

compute_host_runner_version() {
  host_root=$1
  container_root=$2

  run_host_python -c '
import hashlib
import pathlib
import sys

digest = hashlib.sha256()
for arg in sys.argv[1:3]:
    path = pathlib.Path(arg)
    digest.update(path.read_bytes())
for value in sys.argv[3:]:
    digest.update(b"\0")
    digest.update(value.encode("utf-8"))
print(digest.hexdigest())
  ' "$host_runner_script" "$host_runner_bootstrap_script" "$host_root" "$container_root"
}

read_recorded_host_runner_version() {
  if [ ! -f "$host_runner_version_file" ]; then
    return 0
  fi

  tr -d '[:space:]' < "$host_runner_version_file"
}

stop_managed_host_runner() {
  managed_pid=""

  if [ -f "$host_runner_pid_file" ]; then
    existing_pid=$(tr -d '[:space:]' < "$host_runner_pid_file")
    if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
      managed_pid=$existing_pid
    fi
  fi

  if [ -z "$managed_pid" ]; then
    managed_pid=$(find_managed_host_runner_listener_pid || true)
  fi

  if [ -n "$managed_pid" ]; then
    kill "$managed_pid" 2>/dev/null || true
    wait_for_process_exit "$managed_pid" || true
  fi

  rm -f "$host_runner_pid_file"
  remove_host_runner_shared_endpoint_files
  rm -f "$host_runner_version_file"

  return 0
}

start_host_runner() {
  token=$1
  host_runner_root=$(detect_host_runner_root)
  current_host_runner_version=$(compute_host_runner_version "$host_runner_root" "$host_runner_container_root")
  recorded_host_runner_version=$(read_recorded_host_runner_version)
  expected_host_runner_url=$(default_host_runner_url)

  scrub_stale_host_runner_shared_endpoint "$expected_host_runner_url" "$token"

  if [ -f "$host_runner_token_file" ] && [ -s "$host_runner_token_file" ] && host_runner_ready 1; then
    current_listener_pid=$(find_current_host_runner_listener_pid || true)
    if [ -n "$current_listener_pid" ]; then
      printf '%s\n' "$current_listener_pid" > "$host_runner_pid_file"
    fi
    if [ -n "$current_listener_pid" ] && [ "$current_host_runner_version" = "$recorded_host_runner_version" ]; then
      record_host_runner_endpoint "$token" "$preserved_host_runner_timeout"
      return 0
    fi
  fi

  if ! stop_managed_host_runner; then
    return 1
  fi

  host_runner_port=$(choose_host_runner_port "$host_runner_port")

  mkdir -p "$host_runner_request_dir"

  if command -v python3 >/dev/null 2>&1; then
    start_host_runner_supervisor python3
  elif command -v python >/dev/null 2>&1; then
    start_host_runner_supervisor python
  elif command -v py >/dev/null 2>&1; then
    start_host_runner_supervisor py -3
  else
    printf '%s\n' "Could not find a host Python interpreter to start the host runner automatically." >&2
    return 1
  fi

  printf '%s\n' "$runner_pid" > "$host_runner_pid_file"
  if kill -0 "$runner_pid" 2>/dev/null && host_runner_ready 5
  then
    printf '%s\n' "$current_host_runner_version" > "$host_runner_version_file"
    record_host_runner_endpoint "$token" "$preserved_host_runner_timeout"
    return 0
  fi

  kill "$runner_pid" 2>/dev/null || true
  rm -f "$host_runner_pid_file"
  remove_host_runner_shared_endpoint_files
  rm -f "$host_runner_version_file"
  printf '%s\n' "Host runner failed its health check. Check $host_runner_log_file for details." >&2
  return 1
}

workspace_image_tag=$(select_workspace_image_tag)

if running_inside_container; then
  printf '%s\n' "select-workspace-image.sh must run on the host, not inside the dev container." >&2
  exit 1
fi

migrate_repo_local_host_runner_artifacts

host_runner_port=$(read_recorded_host_runner_port)

tmp_file=$(mktemp)
found_tag=0
found_host_runner_engine_host=0
found_host_runner_timeout=0
preserved_host_runner_engine_host=""
preserved_host_runner_timeout=""

# Preserve unrelated .env lines while replacing an existing image tag.
if [ -f "$env_file" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      WORKSPACE_IMAGE_TAG=*)
        printf 'WORKSPACE_IMAGE_TAG=%s\n' "$workspace_image_tag" >> "$tmp_file"
        found_tag=1
        ;;
      DTS_*HOST_RUNNER_URL=*)
        ;;
      DTS_*HOST_RUNNER_TOKEN=*)
        ;;
      DTS_*HOST_RUNNER_REQUESTS_DIR=*)
        ;;
      DTS_*HOST_RUNNER_ENGINE_HOST=*)
        found_host_runner_engine_host=1
        preserved_host_runner_engine_host=${line#*=}
        ;;
      DTS_*HOST_RUNNER_TIMEOUT=*)
        found_host_runner_timeout=1
        preserved_host_runner_timeout=${line#*=}
        ;;
      *)
        printf '%s\n' "$line" >> "$tmp_file"
        ;;
    esac
  done < "$env_file"
fi

# Append the setting for a first run or for a file that did not define it yet.
if [ "$found_tag" -eq 0 ]; then
  printf 'WORKSPACE_IMAGE_TAG=%s\n' "$workspace_image_tag" >> "$tmp_file"
fi

host_runner_token=$(generate_host_runner_token)

if ! start_host_runner "$host_runner_token"; then
  printf '%s\n' "Host runner failed to start automatically. Check $host_runner_log_file for details." >&2
  exit 1
fi

host_runner_url=$(default_host_runner_url)

resolved_host_runner_engine_host="$preserved_host_runner_engine_host"
if [ -z "$resolved_host_runner_engine_host" ]; then
  resolved_host_runner_engine_host="$host_runner_engine_host"
fi

printf 'DTS_HOST_RUNNER_URL=%s\n' "$host_runner_url" >> "$tmp_file"
printf 'DTS_HOST_RUNNER_TOKEN=%s\n' "$host_runner_token" >> "$tmp_file"
printf 'DTS_HOST_RUNNER_ENGINE_HOST=%s\n' "$resolved_host_runner_engine_host" >> "$tmp_file"
printf 'DTS_HOST_RUNNER_REQUESTS_DIR=%s\n' "$host_runner_request_dir" >> "$tmp_file"
if [ "$found_host_runner_timeout" -eq 1 ] && [ -n "$preserved_host_runner_timeout" ]; then
  printf 'DTS_HOST_RUNNER_TIMEOUT=%s\n' "$preserved_host_runner_timeout" >> "$tmp_file"
fi

# Avoid rewriting the file when the selected tag is already current.
if [ ! -f "$env_file" ] || ! cmp -s "$tmp_file" "$env_file"; then
  mv "$tmp_file" "$env_file"
else
  rm "$tmp_file"
fi
