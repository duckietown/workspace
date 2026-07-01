#!/usr/bin/env python3
"""Bootstrap helpers for the host-side Duckietown GUI runner."""

import argparse
import http.client
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HEALTH_PATH = "/healthz"
HEALTH_RESPONSE = b"host-runner:ok"
HTTP_STATUS_OK = 200
HOST_RUNNER_URL_ENV = "DTS_HOST_RUNNER_URL"
HOST_RUNNER_TOKEN_ENV = "DTS_HOST_RUNNER_TOKEN"  # noqa: S105
SUPERVISOR_CHILD_HEALTHCHECK_INTERVAL_SECONDS = 0.5
SUPERVISOR_CHILD_HEALTHCHECK_TIMEOUT_SECONDS = 1
SUPERVISOR_CHILD_STARTUP_GRACE_SECONDS = 5
SUPERVISOR_CHILD_UNHEALTHY_GRACE_SECONDS = 2
LOOPBACK_LISTEN_HOST = "127.0.0.1"


@dataclass(frozen=True)
class _SupervisorConfig:
    script: str
    host_root: str
    container_root: str
    requests_dir: str
    listen_host: str
    listen_port: int
    token: str | None
    restart_delay: float

    def command(self) -> list[str]:
        command = [
            sys.executable,
            self.script,
            "--host-root",
            self.host_root,
            "--container-root",
            self.container_root,
            "--requests-dir",
            self.requests_dir,
            "--listen-host",
            self.listen_host,
            "--listen-port",
            str(self.listen_port),
        ]
        if self.token is not None:
            command.extend(["--token", self.token])
        return command


class _UnsupportedCommandError(SystemExit):

    def __init__(self, command: str) -> None:
        msg = f"Unsupported command {command!r}"
        super().__init__(msg)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Helper utilities for host GUI delegation bootstrap.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("generate-token")

    wait_ready = subcommands.add_parser("wait-ready")
    wait_ready.add_argument("--host", required=True)
    wait_ready.add_argument("--port", required=True, type=int)
    wait_ready.add_argument("--timeout", default=5.0, type=float)

    scrub_endpoint = subcommands.add_parser("scrub-endpoint")
    scrub_endpoint.add_argument("--endpoint-file", required=True)
    scrub_endpoint.add_argument("--expected-url", default="")
    scrub_endpoint.add_argument("--expected-token", default="")
    scrub_endpoint.add_argument("--quiet", action="store_true", default=False)

    select_port_command = subcommands.add_parser("select-port")
    select_port_command.add_argument("--preferred", required=True, type=int)

    supervise_command = subcommands.add_parser("supervise")
    supervise_command.add_argument("--script", required=True)
    supervise_command.add_argument("--host-root", required=True)
    supervise_command.add_argument("--container-root", required=True)
    supervise_command.add_argument("--requests-dir", required=True)
    supervise_command.add_argument("--listen-host", required=True)
    supervise_command.add_argument("--listen-port", required=True, type=int)
    supervise_command.add_argument("--token", required=False, default=None)
    supervise_command.add_argument("--restart-delay", default=1.0, type=float)

    return parser.parse_args()


def _generate_token() -> int:
    token = secrets.token_hex(16)
    sys.stdout.write(token)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def _wait_ready(
    host: str,
    port: int,
    timeout: float,
    *,
    request_timeout: float = 0.5,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection(
                host,
                port,
                timeout=request_timeout,
            )
            try:
                connection.request("GET", HEALTH_PATH)
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()

            if response.status == HTTP_STATUS_OK and body == HEALTH_RESPONSE:
                return 0
        except OSError:
            time.sleep(0.1)
    return 1


def _probe_host_for_listen_host(listen_host: str) -> str:
    if not listen_host:
        return LOOPBACK_LISTEN_HOST
    return listen_host


def _read_env_file(env_file: Path) -> dict[str, str]:
    if not env_file.is_file():
        return {}

    values: dict[str, str] = {}
    env_text = env_file.read_text()
    env_lines = env_text.splitlines()
    for raw_line in env_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _endpoint_manifest_mismatch_reason(
    values: dict[str, str],
    *,
    expected_url: str,
    expected_token: str,
) -> str | None:
    actual_url_value = values.get(HOST_RUNNER_URL_ENV, "")
    actual_token_value = values.get(HOST_RUNNER_TOKEN_ENV, "")
    actual_url = actual_url_value.strip()
    actual_token = actual_token_value.strip()
    normalized_expected_url = expected_url.strip()
    normalized_expected_token = expected_token.strip()

    if normalized_expected_url and actual_url != normalized_expected_url:
        expected_url_repr = repr(normalized_expected_url)
        actual_url_repr = repr(actual_url)
        return (
            f"{HOST_RUNNER_URL_ENV} mismatch: expected "
            f"{expected_url_repr}, found {actual_url_repr}."
        )
    if normalized_expected_token and actual_token != normalized_expected_token:
        expected_token_repr = repr(normalized_expected_token)
        actual_token_repr = repr(actual_token)
        return (
            f"{HOST_RUNNER_TOKEN_ENV} mismatch: expected "
            f"{expected_token_repr}, found {actual_token_repr}."
        )
    return None


def _remove_endpoint_manifest_artifacts(endpoint_file: Path) -> None:
    tmp_endpoint_file = endpoint_file.parent / f"{endpoint_file.name}.tmp"
    for candidate in (endpoint_file, tmp_endpoint_file):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue


def _scrub_endpoint_manifest(
    endpoint_file: str,
    *,
    expected_url: str = "",
    expected_token: str = "",
) -> str | None:
    endpoint_path = Path(endpoint_file)
    endpoint_path = endpoint_path.expanduser()
    endpoint_path = endpoint_path.resolve(strict=False)
    values = _read_env_file(endpoint_path)
    if not values:
        return None

    mismatch_reason = _endpoint_manifest_mismatch_reason(
        values,
        expected_url=expected_url,
        expected_token=expected_token,
    )
    if mismatch_reason is None:
        return None

    _remove_endpoint_manifest_artifacts(endpoint_path)
    return mismatch_reason


def _scrub_endpoint(
    endpoint_file: str,
    expected_url: str,
    expected_token: str,
    *,
    quiet: bool = False,
) -> int:
    mismatch_reason = _scrub_endpoint_manifest(
        endpoint_file,
        expected_url=expected_url,
        expected_token=expected_token,
    )
    if mismatch_reason is None:
        return 0

    if not quiet:
        _write_line(
            "Scrubbed stale host runner endpoint manifest "
            f"{endpoint_file}: {mismatch_reason}",
        )
    return 0


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((LOOPBACK_LISTEN_HOST, port))
    except OSError:
        return False
    return True


def _select_port(preferred: int) -> int:
    selected_port = preferred
    if preferred <= 0 or not _port_is_available(preferred):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((LOOPBACK_LISTEN_HOST, 0))
            socket_name = sock.getsockname()
            selected_port = socket_name[1]

    selected_port_text = str(selected_port)
    sys.stdout.write(selected_port_text)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def _write_line(message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.write("\n")
    sys.stderr.flush()


def _should_handle_sighup() -> bool:
    if not hasattr(signal, "SIGHUP"):
        return False
    return signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN


class _HostRunnerSupervisor:

    def __init__(self, config: _SupervisorConfig) -> None:
        self._config = config
        self.probe_host = _probe_host_for_listen_host(config.listen_host)
        self._process: subprocess.Popen[bytes] | None = None
        self._stop_requested = False

    def install_signal_handlers(self) -> None:
        supported_signals: list[signal.Signals] = [
            signal.SIGINT,
            signal.SIGTERM,
        ]
        if _should_handle_sighup():
            supported_signals.append(signal.SIGHUP)
        for signal_number in supported_signals:
            signal.signal(signal_number, self.handle_signal)

    def handle_signal(self, signal_number: int, frame: object) -> None:
        self._stop_requested = True
        signal_enum = signal.Signals(signal_number)
        signal_name = signal_enum.name
        frame_code = getattr(frame, "f_code", None)
        frame_name = "unknown"
        if frame_code is not None:
            frame_name = frame_code.co_name
        _write_line(
            "Host runner supervisor received "
            f"{signal_name} in {frame_name}; stopping child.",
        )
        self.terminate_child()

    def command(self) -> list[str]:
        return self._config.command()

    def spawn_child(self) -> None:
        command = self.command()
        command_string = " ".join(command)
        _write_line(f"Host runner supervisor starting child: {command_string}")
        self._process = subprocess.Popen(command)  # noqa: S603

    def child_return_code(self) -> int | None:
        process = self._process
        if process is None:
            return None
        return process.poll()

    def child_is_healthy(self) -> bool:
        return _wait_ready(
            self.probe_host,
            self._config.listen_port,
            SUPERVISOR_CHILD_HEALTHCHECK_TIMEOUT_SECONDS,
            request_timeout=SUPERVISOR_CHILD_HEALTHCHECK_TIMEOUT_SECONDS,
        ) == 0

    def wait_for_child_exit(self) -> int:
        startup_deadline = (
            time.monotonic() + SUPERVISOR_CHILD_STARTUP_GRACE_SECONDS
        )
        child_became_healthy = False
        unhealthy_since: float | None = None
        while True:
            return_code = self.child_return_code()
            if return_code is not None:
                return return_code
            if self._stop_requested:
                self.terminate_child()
                continue

            if self.child_is_healthy():
                child_became_healthy = True
                unhealthy_since = None
                time.sleep(SUPERVISOR_CHILD_HEALTHCHECK_INTERVAL_SECONDS)
                continue

            now = time.monotonic()
            if unhealthy_since is None:
                unhealthy_since = now

            if not child_became_healthy:
                if now >= startup_deadline:
                    _write_line(
                        "Host runner child failed to become healthy within "
                        f"{SUPERVISOR_CHILD_STARTUP_GRACE_SECONDS:g}s; "
                        "restarting child.",
                    )
                    self.terminate_child()
                    return self.child_return_code() or 1
                time.sleep(SUPERVISOR_CHILD_HEALTHCHECK_INTERVAL_SECONDS)
                continue

            unhealthy_duration = now - unhealthy_since
            if unhealthy_duration >= SUPERVISOR_CHILD_UNHEALTHY_GRACE_SECONDS:
                _write_line(
                    "Host runner child failed health checks for "
                    f"{unhealthy_duration:.1f}s; restarting child.",
                )
                self.terminate_child()
                return self.child_return_code() or 1

            time.sleep(SUPERVISOR_CHILD_HEALTHCHECK_INTERVAL_SECONDS)

    def terminate_child(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is not None:
            return

        process.terminate()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.2)

        process.kill()
        process.wait()

    def run(self) -> int:
        self.install_signal_handlers()
        while not self._stop_requested:
            self.spawn_child()
            return_code = self.wait_for_child_exit()
            self._process = None
            if self._stop_requested:
                return 0
            _write_line(
                "Host runner child exited unexpectedly with return code "
                f"{return_code}; restarting in "
                f"{self._config.restart_delay:g}s.",
            )
            time.sleep(self._config.restart_delay)
        return 0


def _supervise(config: _SupervisorConfig) -> int:
    supervisor = _HostRunnerSupervisor(config)
    return supervisor.run()


def main() -> int:
    """Run the selected host-runner bootstrap subcommand."""
    args = _parse_args()
    if args.command == "generate-token":
        return _generate_token()
    if args.command == "wait-ready":
        return _wait_ready(args.host, args.port, args.timeout)
    if args.command == "scrub-endpoint":
        return _scrub_endpoint(
            args.endpoint_file,
            args.expected_url,
            args.expected_token,
            quiet=args.quiet,
        )
    if args.command == "select-port":
        return _select_port(args.preferred)
    if args.command == "supervise":
        config = _SupervisorConfig(
            script=args.script,
            host_root=args.host_root,
            container_root=args.container_root,
            requests_dir=args.requests_dir,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            token=args.token,
            restart_delay=args.restart_delay,
        )
        return _supervise(config)
    raise _UnsupportedCommandError(args.command)


if __name__ == "__main__":
    exit_code = main()
    raise SystemExit(exit_code)
