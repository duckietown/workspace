#!/usr/bin/env python3

import argparse
import errno
import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, TextIO, cast

pty_module: Any = None
try:
    import pty as _pty_module
except ImportError:
    pass
else:
    pty_module = _pty_module


SCRIPT_FILE = __file__
SCRIPT_FILE_PATH = Path(SCRIPT_FILE)
SCRIPT_PATH = SCRIPT_FILE_PATH.resolve()
SCRIPT_DIR = SCRIPT_PATH.parent
DEFAULT_REQUESTS_DIR_PATH = Path("/tmp/duckietown/host_runner_requests")

HOST_RUNNER_ACTIVE_ENV = "DTS_HOST_RUNNER_ACTIVE"
HOST_RUNNER_EXIT_CODE_PREFIX = "===DTS_HOST_RUNNER_EXIT_CODE==="
HOST_RUNNER_HEALTH_RESPONSE = b"host-runner:ok"
REQUEST_FILE_SUFFIX = ".request.json"
PROCESSING_FILE_SUFFIX = ".processing.json"
STREAM_FILE_SUFFIX = ".stream"
STREAM_CHUNK_FILE_SUFFIX = ".chunk"
CANCEL_FILE_SUFFIX = ".cancel"
HEARTBEAT_FILE_SUFFIX = ".heartbeat"
DEFAULT_REQUESTS_DIR = str(DEFAULT_REQUESTS_DIR_PATH)
FORWARDED_ENVIRONMENT_MAP = {
    "HOST_DOCKER_REGISTRY": "DOCKER_REGISTRY",
    "HOST_DTSHELL_COMMANDS": "DTSHELL_COMMANDS",
    "HOST_DTS_HOST_RUNNER_ENGINE_HOST": "DTS_HOST_RUNNER_ENGINE_HOST",
    "HOST_DTS_HOST_RUNNER_FRONTEND_URL": "DTS_HOST_RUNNER_FRONTEND_URL",
    "HOST_DTS_HOST_RUNNER_MATRIX_RENDERER_ONLY": "DTS_HOST_RUNNER_MATRIX_RENDERER_ONLY",
}
LOOPBACK_LISTEN_HOSTS = ("127.0.0.1", "::1", "localhost")
REQUEST_COMMAND_EXPECTATION = "a JSON array of non-empty strings"
REQUEST_ARGV_EXPECTATION = "a JSON array of strings"
REQUEST_CWD_EXPECTATION = "a non-empty string"
REQUEST_ENV_EXPECTATION = "a JSON object"
HOST_RUNNER_DTS_EXECUTABLE = "dts"
PROCESS_WAIT_TIMEOUT_SECONDS = 5
REQUEST_WATCHER_HEARTBEAT_TIMEOUT_SECONDS = 3
REQUEST_WATCHER_SUPERVISOR_INTERVAL_SECONDS = 1
REQUEST_CONTROL_POLL_INTERVAL_SECONDS = 0.5
REQUEST_HEARTBEAT_STALE_SECONDS = 6.0


def should_use_pty(env: dict[str, str]) -> bool:
    del env
    return pty_module is not None


class ByteWriter(Protocol):

    def write(self, data: bytes) -> object: ...

    def flush(self) -> object: ...


class ContainerPathError(ValueError):

    def __init__(self, path: str, container_root: str) -> None:
        message = (
            f"Path {path!r} is outside the configured container root "
            f"{container_root!r}."
        )
        super().__init__(message)


class RequestFieldTypeError(TypeError):

    def __init__(self, field: str, expected: str) -> None:
        message = f"Request field {field!r} must be {expected}."
        super().__init__(message)


class RequestFieldValueError(ValueError):

    def __init__(self, field: str, expected: str) -> None:
        message = f"Request field {field!r} must be {expected}."
        super().__init__(message)


class HostWorkingDirectoryError(ValueError):

    def __init__(self, host_cwd: str) -> None:
        message = f"Mapped host working directory {host_cwd!r} does not exist."
        super().__init__(message)


class HostCommandNotFoundError(SystemExit):

    def __init__(self) -> None:
        message = "Could not find the 'dts' executable on the host PATH."
        super().__init__(message)


class HostRootNotFoundError(SystemExit):

    def __init__(self, host_root: str) -> None:
        message = f"Host root {host_root!r} does not exist."
        super().__init__(message)


class ListenerConfigurationError(SystemExit):

    def __init__(self) -> None:
        message = (
            "Refusing to bind a non-loopback host listener without --token."
        )
        super().__init__(message)


class RequestWatcherStatus:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_heartbeat = 0.0

    def record_thread(self, thread: threading.Thread) -> None:
        with self._lock:
            self._thread = thread
            self._last_heartbeat = time.monotonic()

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.monotonic()

    def snapshot(self) -> tuple[bool, float]:
        with self._lock:
            thread = self._thread
            last_heartbeat = self._last_heartbeat

        is_alive = thread is not None and thread.is_alive()
        if last_heartbeat == 0.0:
            return is_alive, float("inf")

        heartbeat_age = time.monotonic() - last_heartbeat
        return is_alive, heartbeat_age


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Accept native Duckietown GUI launch requests from the dev "
            "container and run them on the host."
        ),
    )
    parser.add_argument(
        "--host-root",
        required=True,
        help="Absolute path to the Duckietown workspace root on the host.",
    )
    parser.add_argument(
        "--container-root",
        default="/home/ubuntu/duckietown",
        help=(
            "Absolute path to the same workspace root inside the dev "
            "container."
        ),
    )
    parser.add_argument(
        "--requests-dir",
        default=DEFAULT_REQUESTS_DIR,
        help=(
            "Optional shared directory used for file-based host launch "
            "requests."
        ),
    )
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="Interface to bind the HTTP listener to.",
    )
    parser.add_argument(
        "--listen-port",
        default=59321,
        type=int,
        help="TCP port to bind the HTTP listener to.",
    )
    parser.add_argument(
        "--token",
        default=None,
        type=str,
        help="Optional bearer token required by clients.",
    )
    return parser.parse_args()


def write_line(stream: TextIO, message: str) -> None:
    stream.write(message)
    stream.write("\n")
    stream.flush()


def write_encoded(wfile: ByteWriter, message: str) -> None:
    payload = message.encode(errors="replace")
    wfile.write(payload)


def start_streamed_process(
    command: list[str],
    cwd: str,
    env: dict[str, str],
) -> tuple["subprocess.Popen[Any]", TextIO]:
    if not should_use_pty(env):
        pipe_process = subprocess.Popen(  # noqa: S603
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_stream = pipe_process.stdout
        if output_stream is None:
            msg = "Host-side process stdout pipe was not created."
            raise RuntimeError(msg)
        process_any = cast("subprocess.Popen[Any]", pipe_process)
        output_text_stream = cast("TextIO", output_stream)
        return process_any, output_text_stream

    master_fd, slave_fd = pty_module.openpty()
    try:
        pty_process = subprocess.Popen(  # noqa: S603
            command,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise

    os.close(slave_fd)
    output_stream = os.fdopen(
        master_fd,
        mode="r",
        encoding="utf-8",
        errors="replace",
        buffering=1,
    )
    process_any = cast("subprocess.Popen[Any]", pty_process)
    return process_any, output_stream


def read_stream_segment(stream: TextIO) -> str:
    buffered: list[str] = []
    while True:
        try:
            chunk = stream.read(1)
        except OSError as error:
            eio = getattr(errno, "EIO", None)
            if eio is not None and error.errno == eio:
                chunk = ""
            else:
                raise

        if chunk == "":
            return "".join(buffered)

        buffered.append(chunk)
        if chunk in ("\n", "\r"):
            return "".join(buffered)


def resolve_dts_executable() -> str:
    executable = shutil.which(HOST_RUNNER_DTS_EXECUTABLE)
    if executable is None:
        raise HostCommandNotFoundError()
    return executable


def map_container_path(path: str, container_root: str, host_root: str) -> str:
    normalized_container_root = container_root.rstrip("/\\")
    normalized_path = path.rstrip("/\\")
    host_root_path = Path(host_root)

    if normalized_path == normalized_container_root:
        resolved_host_root_path = host_root_path.resolve(strict=False)
        return str(resolved_host_root_path)

    prefix = f"{normalized_container_root}/"
    if path.startswith(prefix):
        suffix = path[len(prefix):]
        mapped_path = host_root_path / suffix
        resolved_mapped_path = mapped_path.resolve(strict=False)
        return str(resolved_mapped_path)

    raise ContainerPathError(path, container_root)


def map_window_arg_path(
    value: str,
    container_root: str,
    host_root: str,
) -> str:
    if "=" not in value:
        return value

    key, raw_value = value.split("=", 1)
    raw_value_path = Path(raw_value)
    if not raw_value or not raw_value_path.is_absolute():
        return value

    try:
        mapped_value = map_container_path(raw_value, container_root, host_root)
    except ContainerPathError:
        return value

    return f"{key}={mapped_value}"


def map_plain_arg_path(
    value: str,
    container_root: str,
    host_root: str,
) -> str:
    value_path = Path(value)
    if not value or not value_path.is_absolute():
        return value

    try:
        return map_container_path(value, container_root, host_root)
    except ContainerPathError:
        return value


def map_command_argv(
    argv: list[str],
    container_root: str,
    host_root: str,
) -> list[str]:
    mapped_argv: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        mapped_argv.append(arg)
        if arg == "--renderer-binary" and index + 1 < len(argv):
            mapped_value = map_plain_arg_path(
                argv[index + 1],
                container_root,
                host_root,
            )
            mapped_argv.append(mapped_value)
            index += 2
            continue
        if arg == "--window-arg" and index + 1 < len(argv):
            mapped_value = map_window_arg_path(
                argv[index + 1],
                container_root,
                host_root,
            )
            mapped_argv.append(mapped_value)
            index += 2
            continue

        index += 1

    return mapped_argv


def build_child_environment(
    forwarded_env: dict[str, object],
) -> dict[str, str]:
    env = os.environ.copy()
    env[HOST_RUNNER_ACTIVE_ENV] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    for key, value in forwarded_env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        target_key = FORWARDED_ENVIRONMENT_MAP.get(key)
        if target_key is None:
            continue
        env[target_key] = value

    return env


def validate_command_path(command: object) -> tuple[str, ...]:
    if not isinstance(command, list):
        raise RequestFieldTypeError("command", REQUEST_COMMAND_EXPECTATION)

    command_tuple = tuple(command)
    if not all(isinstance(part, str) for part in command_tuple):
        raise RequestFieldTypeError("command", REQUEST_COMMAND_EXPECTATION)
    if not command_tuple or any(not part for part in command_tuple):
        raise RequestFieldValueError("command", REQUEST_COMMAND_EXPECTATION)

    return command_tuple


def parse_request_payload(
    payload: object,
    config: argparse.Namespace,
) -> tuple[list[str], str, dict[str, str], bool]:
    if not isinstance(payload, dict):
        raise RequestFieldTypeError("request", REQUEST_ENV_EXPECTATION)

    command = payload.get("command")
    argv = payload.get("argv", [])
    cwd = payload.get("cwd")
    forwarded_env = payload.get("env", {})

    if not isinstance(argv, list):
        raise RequestFieldTypeError("argv", REQUEST_ARGV_EXPECTATION)
    if not all(isinstance(arg, str) for arg in argv):
        raise RequestFieldTypeError("argv", REQUEST_ARGV_EXPECTATION)
    if not isinstance(cwd, str):
        raise RequestFieldTypeError("cwd", REQUEST_CWD_EXPECTATION)
    if not cwd:
        raise RequestFieldValueError("cwd", REQUEST_CWD_EXPECTATION)
    if not isinstance(forwarded_env, dict):
        raise RequestFieldTypeError("env", REQUEST_ENV_EXPECTATION)

    command_path = validate_command_path(command)
    host_cwd = map_container_path(cwd, config.container_root, config.host_root)
    host_cwd_path = Path(host_cwd)
    if not host_cwd_path.is_dir():
        raise HostWorkingDirectoryError(host_cwd)

    child_env = build_child_environment(forwarded_env)
    mapped_argv = map_command_argv(
        argv,
        config.container_root,
        config.host_root,
    )
    command_list = [config.dts_executable, *command_path, *mapped_argv]
    emit_launch_context = "--verbose" in mapped_argv or "-vv" in mapped_argv
    return command_list, host_cwd, child_env, emit_launch_context


class FileStreamWriter:

    def __init__(self, stream_path: Path) -> None:
        self._stream_path = stream_path
        self._chunk_index = 0
        self._pending = bytearray()

    def write(self, data: bytes) -> None:
        self._pending.extend(data)

    def flush(self) -> None:
        if not self._pending:
            return
        chunk_name = (
            f"{self._stream_path.name}."
            f"{self._chunk_index:08d}"
            f"{STREAM_CHUNK_FILE_SUFFIX}"
        )
        chunk_path = self._stream_path.with_name(chunk_name)
        with chunk_path.open("wb") as stream:
            stream.write(self._pending)
            stream.flush()
            file_descriptor = stream.fileno()
            os.fsync(file_descriptor)
        self._chunk_index += 1
        self._pending.clear()
        directory_fd: int | None = None
        try:
            directory_fd = os.open(self._stream_path.parent, os.O_RDONLY)
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def close(self) -> None:
        self.flush()


def request_cancel_path(request_path: Path) -> Path:
    request_base_path = request_path.with_suffix("")
    request_base_path = request_base_path.with_suffix("")
    return request_base_path.with_name(request_base_path.name + CANCEL_FILE_SUFFIX)


def request_heartbeat_path(request_path: Path) -> Path:
    request_base_path = request_path.with_suffix("")
    request_base_path = request_base_path.with_suffix("")
    return request_base_path.with_name(
        request_base_path.name + HEARTBEAT_FILE_SUFFIX
    )


def request_heartbeat_is_stale(
    heartbeat_path: Path,
    *,
    stale_after_seconds: float = REQUEST_HEARTBEAT_STALE_SECONDS,
) -> bool:
    try:
        heartbeat_mtime = heartbeat_path.stat().st_mtime
    except FileNotFoundError:
        return True

    heartbeat_age = time.time() - heartbeat_mtime
    return heartbeat_age > stale_after_seconds


def request_process_shutdown(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return

    with suppress(OSError, ValueError):
        process.send_signal(signal.SIGINT)

    deadline = time.monotonic() + PROCESS_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)

    if process.poll() is None:
        process.terminate()

    deadline = time.monotonic() + PROCESS_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)

    if process.poll() is None:
        process.kill()
        process.wait()


def watch_for_cancellation(
    process: subprocess.Popen[Any],
    cancel_path: Path,
    heartbeat_path: Path,
) -> None:
    while process.poll() is None:
        if cancel_path.exists():
            request_process_shutdown(process)
            return
        if request_heartbeat_is_stale(heartbeat_path):
            stale_message = (
                "Host runner request heartbeat went stale for "
                f"{heartbeat_path.name}; stopping child."
            )
            write_line(sys.stderr, stale_message)
            request_process_shutdown(process)
            return
        time.sleep(REQUEST_CONTROL_POLL_INTERVAL_SECONDS)


def process_request_file(
    request_path: Path,
    config: argparse.Namespace,
) -> None:
    stream_base_path = request_path.with_suffix("")
    stream_base_path = stream_base_path.with_suffix("")
    stream_path = stream_base_path.with_name(
        stream_base_path.name + STREAM_FILE_SUFFIX,
    )
    cancel_path = request_cancel_path(request_path)
    heartbeat_path = request_heartbeat_path(request_path)

    writer = FileStreamWriter(stream_path)
    try:
        payload_text = request_path.read_text(encoding="utf-8")
        payload = json.loads(payload_text)
        (
            command,
            host_cwd,
            child_env,
            emit_launch_context,
        ) = parse_request_payload(payload, config)
        stream_process_output(
            command,
            host_cwd,
            child_env,
            writer,
            cancel_path=cancel_path,
            heartbeat_path=heartbeat_path,
            emit_launch_context=emit_launch_context,
        )
    except Exception as error:  # noqa: BLE001
        error_message = f"Host runner request failed: {error}\n"
        write_encoded(writer, error_message)
        exit_code_message = f"{HOST_RUNNER_EXIT_CODE_PREFIX}1\n"
        writer.write(exit_code_message.encode())
        writer.flush()
    finally:
        writer.close()
        request_path.unlink(missing_ok=True)
        cancel_path.unlink(missing_ok=True)
        heartbeat_path.unlink(missing_ok=True)


def start_request_processor(
    request_path: Path,
    config: argparse.Namespace,
) -> None:
    worker_id = uuid.uuid4()
    worker_suffix = worker_id.hex[:8]
    worker_name = f"host-runner-request-{worker_suffix}"
    request_thread = threading.Thread(
        target=process_request_file,
        args=(request_path, config),
        daemon=True,
        name=worker_name,
    )
    request_thread.start()


def serve_request_directory(
    config: argparse.Namespace,
    status: RequestWatcherStatus,
) -> None:
    if config.requests_dir is None:
        return

    requests_dir = Path(config.requests_dir)
    requests_dir.mkdir(parents=True, exist_ok=True)
    watch_message = (
        f"Host runner watching requests dir {requests_dir}"
    )
    write_line(sys.stderr, watch_message)

    while True:
        status.heartbeat()
        try:
            pattern = f"*{REQUEST_FILE_SUFFIX}"
            request_paths = sorted(requests_dir.glob(pattern))
            for request_path in request_paths:
                claimed_name = request_path.name[: -len(REQUEST_FILE_SUFFIX)]
                claimed_name += PROCESSING_FILE_SUFFIX
                claimed_path = request_path.with_name(claimed_name)
                try:
                    request_path.rename(claimed_path)
                except FileNotFoundError:
                    continue
                except OSError:
                    continue

                claim_message = (
                    f"Host runner claimed request "
                    f"{claimed_path.name}"
                )
                write_line(sys.stderr, claim_message)
                start_request_processor(claimed_path, config)
        except Exception as error:  # noqa: BLE001
            failure_message = (
                f"Host runner request watcher failed: {error}"
            )
            write_line(sys.stderr, failure_message)

        status.heartbeat()
        time.sleep(0.1)


def stream_process_output(
    command: Iterable[str],
    cwd: str,
    env: dict[str, str],
    wfile: ByteWriter,
    *,
    cancel_path: Path | None = None,
    heartbeat_path: Path | None = None,
    emit_launch_context: bool = False,
) -> None:
    command_list = list(command)
    if emit_launch_context:
        cwd_message = f"[host-runner] cwd: {cwd}\n"
        write_encoded(wfile, cwd_message)
        dtshell_commands = env.get("DTSHELL_COMMANDS")
        if dtshell_commands:
            commands_message = (
                "[host-runner] DTSHELL_COMMANDS="
                f"{dtshell_commands}\n"
            )
            write_encoded(wfile, commands_message)
        exec_message = (
            "[host-runner] exec: "
            f"{shlex.join(command_list)}\n"
        )
        write_encoded(wfile, exec_message)
        wfile.flush()

    if cancel_path is not None and cancel_path.exists():
        exit_code_message = f"{HOST_RUNNER_EXIT_CODE_PREFIX}130\n"
        wfile.write(exit_code_message.encode())
        wfile.flush()
        return
    if heartbeat_path is not None and request_heartbeat_is_stale(heartbeat_path):
        exit_code_message = f"{HOST_RUNNER_EXIT_CODE_PREFIX}130\n"
        wfile.write(exit_code_message.encode())
        wfile.flush()
        return

    try:
        process, stdout_stream = start_streamed_process(command_list, cwd, env)
    except OSError as error:
        command_string = shlex.join(command_list)
        launch_error_message = (
            f"Failed to launch host-side '{command_string}': {error}\n"
        )
        write_encoded(wfile, launch_error_message)
        exit_code_message = f"{HOST_RUNNER_EXIT_CODE_PREFIX}127\n"
        wfile.write(exit_code_message.encode())
        wfile.flush()
        return

    cancellation_thread: threading.Thread | None = None
    if cancel_path is not None:
        cancellation_thread = threading.Thread(
            target=watch_for_cancellation,
            args=(process, cancel_path, heartbeat_path),
            daemon=True,
            name=f"host-runner-cancel-{process.pid}",
        )
        cancellation_thread.start()

    try:
        while True:
            segment = read_stream_segment(stdout_stream)
            if not segment:
                break
            write_encoded(wfile, segment)
            wfile.flush()
    except BrokenPipeError:
        request_process_shutdown(process)
        return
    finally:
        stdout_stream.close()
        if cancellation_thread is not None:
            cancellation_thread.join(timeout=0.2)

    return_code = process.wait()
    exit_code_message = f"{HOST_RUNNER_EXIT_CODE_PREFIX}{return_code}\n"
    wfile.write(exit_code_message.encode())
    wfile.flush()


def make_handler(
    config: argparse.Namespace,
) -> type[BaseHTTPRequestHandler]:
    class HostRunnerHandler(BaseHTTPRequestHandler):

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/healthz":
                request_watcher_healthy, health_message = request_watcher_ready(config)
                if not request_watcher_healthy:
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    write_encoded(self.wfile, health_message)
                    self.wfile.flush()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(HOST_RUNNER_HEALTH_RESPONSE)
                self.wfile.flush()
                return

            self.send_error(404, "Not found")

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/run":
                self.send_error(404, "Not found")
                return

            if config.token is not None:
                expected = f"Bearer {config.token}"
                if self.headers.get("Authorization") != expected:
                    self.send_error(403, "Forbidden")
                    return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                payload_bytes = self.rfile.read(content_length)
                payload_text = payload_bytes.decode()
                payload = json.loads(payload_text)
                (
                    command,
                    host_cwd,
                    child_env,
                    emit_launch_context,
                ) = parse_request_payload(payload, config)
            except (TypeError, ValueError) as error:
                self.send_error(400, str(error))
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            stream_process_output(
                command,
                host_cwd,
                child_env,
                self.wfile,
                emit_launch_context=emit_launch_context,
            )

        def log_message(
            self,
            message_format: str,
            *args: object,
        ) -> None:
            message = message_format % args
            prefix = "host-runner: "
            write_line(sys.stderr, prefix + message)

    return HostRunnerHandler


def request_watcher_ready(config: argparse.Namespace) -> tuple[bool, str]:
    if config.requests_dir is None:
        return True, ""

    status = getattr(config, "request_watcher_status", None)
    if not isinstance(status, RequestWatcherStatus):
        return False, "Host runner request watcher status is unavailable."

    watcher_alive, heartbeat_age = status.snapshot()
    if not watcher_alive:
        return False, "Host runner request watcher is not running."

    if heartbeat_age > REQUEST_WATCHER_HEARTBEAT_TIMEOUT_SECONDS:
        return (
            False,
            "Host runner request watcher heartbeat is stale after "
            f"{heartbeat_age:.1f}s.",
        )

    return True, ""


def launch_request_watcher(
    config: argparse.Namespace,
    status: RequestWatcherStatus,
) -> None:
    watcher_id = uuid.uuid4()
    watcher_suffix = watcher_id.hex[:8]
    thread_name = f"host-runner-file-{watcher_suffix}"
    request_thread = threading.Thread(
        target=serve_request_directory,
        args=(config, status),
        daemon=True,
        name=thread_name,
    )
    request_thread.start()
    status.record_thread(request_thread)


def supervise_request_watcher(
    config: argparse.Namespace,
    status: RequestWatcherStatus,
) -> None:
    stale_warning_emitted = False

    while True:
        watcher_alive, heartbeat_age = status.snapshot()
        if not watcher_alive:
            write_line(sys.stderr, "Host runner request watcher stopped; restarting.")
            launch_request_watcher(config, status)
            stale_warning_emitted = False
        elif heartbeat_age > REQUEST_WATCHER_HEARTBEAT_TIMEOUT_SECONDS:
            if not stale_warning_emitted:
                stall_message = (
                    "Host runner request watcher heartbeat is stale after "
                    f"{heartbeat_age:.1f}s."
                )
                write_line(sys.stderr, stall_message)
                stale_warning_emitted = True
        else:
            stale_warning_emitted = False

        time.sleep(REQUEST_WATCHER_SUPERVISOR_INTERVAL_SECONDS)


def start_request_watcher(config: argparse.Namespace) -> RequestWatcherStatus:
    status = RequestWatcherStatus()
    launch_request_watcher(config, status)

    supervisor_id = uuid.uuid4()
    supervisor_suffix = supervisor_id.hex[:8]
    thread_name = f"host-runner-watchdog-{supervisor_suffix}"
    supervisor_thread = threading.Thread(
        target=supervise_request_watcher,
        args=(config, status),
        daemon=True,
        name=thread_name,
    )
    supervisor_thread.start()

    return status


def main() -> int:
    args = parse_args()
    host_root_path = Path(args.host_root)
    host_root_path = host_root_path.expanduser()
    host_root_path = host_root_path.resolve(strict=False)
    args.host_root = str(host_root_path)

    if args.requests_dir is not None:
        requests_dir_path = Path(args.requests_dir)
        requests_dir_path = requests_dir_path.expanduser()
        requests_dir_path = requests_dir_path.resolve(strict=False)
        args.requests_dir = str(requests_dir_path)

    if not host_root_path.is_dir():
        raise HostRootNotFoundError(args.host_root)

    is_loopback_listener = args.listen_host in LOOPBACK_LISTEN_HOSTS
    if args.token is None and not is_loopback_listener:
        raise ListenerConfigurationError()

    args.dts_executable = resolve_dts_executable()

    if args.requests_dir is not None:
        requests_dir_path = Path(args.requests_dir)
        requests_dir_path.mkdir(parents=True, exist_ok=True)
        request_watcher_status = start_request_watcher(args)
        args.request_watcher_status = request_watcher_status

    handler = make_handler(args)
    server = ThreadingHTTPServer(
        (args.listen_host, args.listen_port),
        handler,
    )
    listen_message = (
        "Host runner listening on "
        f"http://{args.listen_host}:{args.listen_port}/run"
        f" with requests dir {args.requests_dir}"
    )
    write_line(sys.stdout, listen_message)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
