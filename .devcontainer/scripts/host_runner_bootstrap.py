#!/usr/bin/env python3

import argparse
import http.client
import secrets
import socket
import subprocess
import signal
import sys
import time
from pathlib import Path


HEALTH_PATH = "/healthz"
HEALTH_RESPONSE = b"host-runner:ok"
HOST_RUNNER_URL_ENV = "DTS_HOST_RUNNER_URL"
HOST_RUNNER_TOKEN_ENV = "DTS_HOST_RUNNER_TOKEN"
SUPERVISOR_CHILD_HEALTHCHECK_INTERVAL_SECONDS = 0.5
SUPERVISOR_CHILD_HEALTHCHECK_TIMEOUT_SECONDS = 1.0
SUPERVISOR_CHILD_STARTUP_GRACE_SECONDS = 5.0
SUPERVISOR_CHILD_UNHEALTHY_GRACE_SECONDS = 2.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Helper utilities for host GUI delegation bootstrap."
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


def generate_token() -> int:
    sys.stdout.write(secrets.token_hex(16))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def wait_ready(
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

            if response.status == 200 and body == HEALTH_RESPONSE:
                return 0
        except OSError:
            time.sleep(0.1)
    return 1


def probe_host_for_listen_host(listen_host: str) -> str:
    if listen_host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return listen_host


def read_env_file(env_file: Path) -> dict[str, str]:
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


def endpoint_manifest_mismatch_reason(
    values: dict[str, str],
    *,
    expected_url: str,
    expected_token: str,
) -> str | None:
    actual_url = values.get(HOST_RUNNER_URL_ENV, "").strip()
    actual_token = values.get(HOST_RUNNER_TOKEN_ENV, "").strip()
    normalized_expected_url = expected_url.strip()
    normalized_expected_token = expected_token.strip()

    if normalized_expected_url and actual_url != normalized_expected_url:
        return (
            f"{HOST_RUNNER_URL_ENV} mismatch: expected {normalized_expected_url!r}, "
            f"found {actual_url!r}."
        )
    if normalized_expected_token and actual_token != normalized_expected_token:
        return (
            f"{HOST_RUNNER_TOKEN_ENV} mismatch: expected {normalized_expected_token!r}, "
            f"found {actual_token!r}."
        )
    return None


def remove_endpoint_manifest_artifacts(endpoint_file: Path) -> None:
    tmp_endpoint_file = endpoint_file.parent / f"{endpoint_file.name}.tmp"
    for candidate in (endpoint_file, tmp_endpoint_file):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue


def scrub_endpoint_manifest(
    endpoint_file: str,
    *,
    expected_url: str = "",
    expected_token: str = "",
) -> str | None:
    endpoint_path = Path(endpoint_file)
    endpoint_path = endpoint_path.expanduser()
    endpoint_path = endpoint_path.resolve(strict=False)
    values = read_env_file(endpoint_path)
    if not values:
        return None

    mismatch_reason = endpoint_manifest_mismatch_reason(
        values,
        expected_url=expected_url,
        expected_token=expected_token,
    )
    if mismatch_reason is None:
        return None

    remove_endpoint_manifest_artifacts(endpoint_path)
    return mismatch_reason


def scrub_endpoint(
    endpoint_file: str,
    expected_url: str,
    expected_token: str,
    quiet: bool = False,
) -> int:
    mismatch_reason = scrub_endpoint_manifest(
        endpoint_file,
        expected_url=expected_url,
        expected_token=expected_token,
    )
    if mismatch_reason is None:
        return 0

    if not quiet:
        write_line(
            "Scrubbed stale host runner endpoint manifest "
            f"{endpoint_file}: {mismatch_reason}"
        )
    return 0


def port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("", port))
    except OSError:
        return False
    return True


def select_port(preferred: int) -> int:
    selected_port = preferred
    if preferred <= 0 or not port_is_available(preferred):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("", 0))
            selected_port = int(sock.getsockname()[1])

    sys.stdout.write(str(selected_port))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def write_line(message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.write("\n")
    sys.stderr.flush()


def should_handle_sighup() -> bool:
    if not hasattr(signal, "SIGHUP"):
        return False
    return signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN


class HostRunnerSupervisor:

    def __init__(
        self,
        *,
        script: str,
        host_root: str,
        container_root: str,
        requests_dir: str,
        listen_host: str,
        listen_port: int,
        token: str | None,
        restart_delay: float,
    ) -> None:
        self.script = script
        self.host_root = host_root
        self.container_root = container_root
        self.requests_dir = requests_dir
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.token = token
        self.restart_delay = restart_delay
        self.probe_host = probe_host_for_listen_host(listen_host)
        self._process: subprocess.Popen[bytes] | None = None
        self._stop_requested = False

    def install_signal_handlers(self) -> None:
        supported_signals: list[signal.Signals] = [signal.SIGINT, signal.SIGTERM]
        if should_handle_sighup():
            supported_signals.append(signal.SIGHUP)
        for signal_number in supported_signals:
            signal.signal(signal_number, self.handle_signal)

    def handle_signal(self, signal_number: int, _frame: object) -> None:
        self._stop_requested = True
        signal_name = signal.Signals(signal_number).name
        write_line(f"Host runner supervisor received {signal_name}; stopping child.")
        self.terminate_child()

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

    def spawn_child(self) -> None:
        command = self.command()
        command_string = " ".join(command)
        write_line(f"Host runner supervisor starting child: {command_string}")
        self._process = subprocess.Popen(command)

    def child_return_code(self) -> int | None:
        process = self._process
        if process is None:
            return None
        return process.poll()

    def child_is_healthy(self) -> bool:
        return wait_ready(
            self.probe_host,
            self.listen_port,
            SUPERVISOR_CHILD_HEALTHCHECK_TIMEOUT_SECONDS,
            request_timeout=SUPERVISOR_CHILD_HEALTHCHECK_TIMEOUT_SECONDS,
        ) == 0

    def wait_for_child_exit(self) -> int:
        startup_deadline = time.monotonic() + SUPERVISOR_CHILD_STARTUP_GRACE_SECONDS
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
                    write_line(
                        "Host runner child failed to become healthy within "
                        f"{SUPERVISOR_CHILD_STARTUP_GRACE_SECONDS:g}s; restarting child."
                    )
                    self.terminate_child()
                    return self.child_return_code() or 1
                time.sleep(SUPERVISOR_CHILD_HEALTHCHECK_INTERVAL_SECONDS)
                continue

            unhealthy_duration = now - unhealthy_since
            if unhealthy_duration >= SUPERVISOR_CHILD_UNHEALTHY_GRACE_SECONDS:
                write_line(
                    "Host runner child failed health checks for "
                    f"{unhealthy_duration:.1f}s; restarting child."
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
            write_line(
                "Host runner child exited unexpectedly with return code "
                f"{return_code}; restarting in {self.restart_delay:g}s."
            )
            time.sleep(self.restart_delay)
        return 0


def supervise(
    *,
    script: str,
    host_root: str,
    container_root: str,
    requests_dir: str,
    listen_host: str,
    listen_port: int,
    token: str | None,
    restart_delay: float,
) -> int:
    supervisor = HostRunnerSupervisor(
        script=script,
        host_root=host_root,
        container_root=container_root,
        requests_dir=requests_dir,
        listen_host=listen_host,
        listen_port=listen_port,
        token=token,
        restart_delay=restart_delay,
    )
    return supervisor.run()


def main() -> int:
    args = parse_args()
    if args.command == "generate-token":
        return generate_token()
    if args.command == "wait-ready":
        return wait_ready(args.host, args.port, args.timeout)
    if args.command == "scrub-endpoint":
        return scrub_endpoint(
            args.endpoint_file,
            args.expected_url,
            args.expected_token,
            args.quiet,
        )
    if args.command == "select-port":
        return select_port(args.preferred)
    if args.command == "supervise":
        return supervise(
            script=args.script,
            host_root=args.host_root,
            container_root=args.container_root,
            requests_dir=args.requests_dir,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            token=args.token,
            restart_delay=args.restart_delay,
        )
    raise ValueError(f"Unsupported command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
