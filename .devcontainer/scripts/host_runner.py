#!/usr/bin/env python3
# ruff: noqa: D100, D101, D102, D103, D107

import argparse
import errno
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol, TextIO, cast

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
DEFAULT_REQUESTS_DIR_PATH = Path("/tmp/duckietown/host_runner_requests")  # noqa: S108
REQUESTS_DIR_ROOT_PATH = DEFAULT_REQUESTS_DIR_PATH.parent

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
    "HOST_DTS_HOST_RUNNER_MATRIX_RENDERER_ONLY": (
        "DTS_HOST_RUNNER_MATRIX_RENDERER_ONLY"
    ),
}
LOOPBACK_LISTEN_HOSTS = ("127.0.0.1", "::1", "localhost")
REQUEST_COMMAND_FIELD = "command"
REQUEST_ARGV_FIELD = "argv"
REQUEST_CWD_FIELD = "cwd"
REQUEST_ENV_FIELD = "env"
REQUEST_COMMAND_EXPECTATION = "a JSON array of non-empty strings"
REQUEST_ARGV_EXPECTATION = "a JSON array of strings"
REQUEST_CWD_EXPECTATION = "a non-empty string"
REQUEST_ENV_EXPECTATION = "a JSON object"
HOST_RUNNER_DTS_EXECUTABLE = "dts"
ALLOWED_DELEGATED_DTS_COMMANDS = (
    ("matrix", "run"),
    ("duckiebot", "image_viewer"),
    ("duckiebot", "keyboard_control"),
    ("duckiebot", "calibrate_intrinsics"),
    ("duckiebot", "calibrate_extrinsics"),
    ("duckiebot", "led_control"),
    ("duckiebot", "graph_plotter"),
)
ALLOWED_DTS_CLI_OPTIONS = {"--debug", "--verbose", "-vv", "--quiet", "-q"}
PROCESS_WAIT_TIMEOUT_SECONDS = 5
REQUEST_WATCHER_HEARTBEAT_TIMEOUT_SECONDS = 3
REQUEST_WATCHER_SUPERVISOR_INTERVAL_SECONDS = 1
REQUEST_CONTROL_POLL_INTERVAL_SECONDS = 0.5
REQUEST_HEARTBEAT_STALE_SECONDS = 6
HOST_LABEL_MAX_LENGTH = 63
MAX_PORT_NUMBER = 65535
MAX_RENDERER_ID = 2147483647
HOST_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
VIEWER_BOOLEAN_ARGS = {
    "--enable-hardware-acceleration",
    "--fullscreen",
    "--no-pull",
    "--on-top",
    "--verbose",
}
MATRIX_BOOLEAN_ARG_MAP = {
    "--force-opengl": "--force-opengl",
    "--force-vulkan": "--force-vulkan",
    "--no-tutorial": "--no-tutorial",
    "--profiler": "--profiler",
    "--verbose": "--verbose",
    "-gl": "--force-opengl",
    "-vk": "--force-vulkan",
    "-vv": "--verbose",
}
MATRIX_IGNORED_FLAGS = {
    "--embedded",
    "--expose-ports",
    "--no-pull",
    "--sandbox",
    "--standalone",
    "--static-ports",
    "-S",
    "-s",
}
OS_FAMILY_VALUES = ("linux", "macos", "windows")


def should_use_pty() -> bool:
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


class HostPathEscapeError(ValueError):
    def __init__(self, path: str, host_root: str) -> None:
        message = (
            f"Mapped host path for {path!r} escapes the configured "
            f"host root {host_root!r}."
        )
        super().__init__(message)


class HostMappedPathNotFoundError(ValueError):

    def __init__(self, path: str) -> None:
        message = f"Mapped host path for {path!r} does not exist."
        super().__init__(message)


class DelegatedArgumentError(ValueError):

    def __init__(self, message: str) -> None:
        super().__init__(message)


class RequestQueuePathError(ValueError):

    def __init__(self, message: str) -> None:
        super().__init__(message)


def delegated_argument_error(message: str) -> DelegatedArgumentError:
    return DelegatedArgumentError(message)


def invalid_option_value_error(
    option_name: str,
    description: str,
) -> DelegatedArgumentError:
    message = f"{option_name} must be {description}."
    return delegated_argument_error(message)


def invalid_hostname_or_ip_error(option_name: str) -> DelegatedArgumentError:
    message = f"{option_name} must be a valid hostname or IP address."
    return delegated_argument_error(message)


def missing_option_value_error(option_name: str) -> DelegatedArgumentError:
    message = f"{option_name} requires a value."
    return delegated_argument_error(message)


def invalid_delegated_arguments_error(
    command: tuple[str, ...],
    detail: str,
) -> DelegatedArgumentError:
    command_text = " ".join(command)
    message = f"Invalid delegated arguments for '{command_text}': {detail}."
    return delegated_argument_error(message)


def option_must_be_one_of_error(
    option_name: str,
    values: tuple[str, ...],
) -> DelegatedArgumentError:
    values_text = ", ".join(values)
    message = f"{option_name} must be one of {values_text}."
    return delegated_argument_error(message)


def option_not_permitted_error(option_name: str) -> DelegatedArgumentError:
    message = f"{option_name} is not permitted in delegated matrix runs."
    return delegated_argument_error(message)


def request_queue_path_error(message: str) -> RequestQueuePathError:
    return RequestQueuePathError(message)


def invalid_request_id_error(request_id: str) -> RequestQueuePathError:
    message = f"Invalid host-runner request id {request_id!r}."
    return request_queue_path_error(message)


def symlinked_request_artifact_error(name: str) -> RequestQueuePathError:
    message = f"Refusing symlinked host-runner artifact {name!r}."
    return request_queue_path_error(message)


def request_artifact_outside_queue_error(name: str) -> RequestQueuePathError:
    message = (
        f"Host-runner artifact {name!r} is outside the request queue."
    )
    return request_queue_path_error(message)


def unexpected_request_artifact_suffix_error(
    artifact_name: str,
) -> RequestQueuePathError:
    message = (
        f"Host-runner artifact {artifact_name!r} has an unexpected suffix."
    )
    return request_queue_path_error(message)


class HostCommandNotFoundError(SystemExit):

    def __init__(self) -> None:
        message = "Could not find the 'dts' executable on the host PATH."
        super().__init__(message)


class HostRootNotFoundError(SystemExit):

    def __init__(self, host_root: str) -> None:
        message = f"Host root {host_root!r} does not exist."
        super().__init__(message)


class RequestsDirConfigurationError(SystemExit):

    def __init__(self, requests_dir: str, expected_dir: str) -> None:
        message = (
            f"Host runner requests dir {requests_dir!r} must be "
            f"{expected_dir!r}."
        )
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
        self._last_heartbeat: float = 0.0

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
        if last_heartbeat == 0:
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
    if not should_use_pty():
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
        raise HostCommandNotFoundError
    return executable


def ensure_path_is_within_host_root(
    path: str,
    mapped_path: Path,
    resolved_host_root_path: Path,
) -> None:
    try:
        mapped_path.relative_to(resolved_host_root_path)
    except ValueError as error:
        host_root_text = str(resolved_host_root_path)
        raise HostPathEscapeError(
            path,
            host_root_text,
        ) from error


def ensure_default_requests_dir(requests_dir: str) -> None:
    if requests_dir != DEFAULT_REQUESTS_DIR:
        raise RequestsDirConfigurationError(
            requests_dir,
            DEFAULT_REQUESTS_DIR,
        )


def relative_container_path(
    path: str,
    container_root: str,
) -> PurePosixPath:
    container_path = PurePosixPath(path)
    container_root_path = PurePosixPath(container_root)
    try:
        relative_path = container_path.relative_to(container_root_path)
    except ValueError as error:
        raise ContainerPathError(path, container_root) from error

    if any(part == ".." for part in relative_path.parts):
        raise ContainerPathError(path, container_root)

    return relative_path


def map_container_path(path: str, container_root: str, host_root: str) -> str:
    host_root_path = Path(host_root)
    resolved_host_root_path = host_root_path.resolve(strict=True)

    if path.rstrip("/\\") == container_root.rstrip("/\\"):
        return str(resolved_host_root_path)

    relative_path = relative_container_path(path, container_root)
    mapped_path = host_root_path.joinpath(*relative_path.parts)
    try:
        resolved_mapped_path = mapped_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise HostMappedPathNotFoundError(path) from error
    ensure_path_is_within_host_root(
        path,
        resolved_mapped_path,
        resolved_host_root_path,
    )
    return str(resolved_mapped_path)


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
        raise RequestFieldTypeError(
            REQUEST_COMMAND_FIELD,
            REQUEST_COMMAND_EXPECTATION,
        )

    command_tuple = tuple(command)
    if not all(isinstance(part, str) for part in command_tuple):
        raise RequestFieldTypeError(
            REQUEST_COMMAND_FIELD,
            REQUEST_COMMAND_EXPECTATION,
        )
    if not command_tuple or any(not part for part in command_tuple):
        raise RequestFieldValueError(
            REQUEST_COMMAND_FIELD,
            REQUEST_COMMAND_EXPECTATION,
        )

    return command_tuple


def validate_delegated_command_path(command: object) -> tuple[str, ...]:
    command_tuple = validate_command_path(command)
    command_index = 0
    sanitized_command: list[str] = []

    while command_index < len(command_tuple):
        part = command_tuple[command_index]
        if not part.startswith("-"):
            break

        if part in ALLOWED_DTS_CLI_OPTIONS:
            sanitized_command.append(part)
            command_index += 1
            continue

        if part == "--profile":
            if command_index + 1 >= len(command_tuple):
                break
            profile_name = validate_pattern_value(
                command_tuple[command_index + 1],
                PROFILE_NAME_PATTERN,
                option_name="--profile",
                description="a simple profile name",
            )
            sanitized_command.extend([part, profile_name])
            command_index += 2
            continue

        if part.startswith("--profile=") and part != "--profile=":
            profile_name = validate_pattern_value(
                part[len("--profile=") :],
                PROFILE_NAME_PATTERN,
                option_name="--profile",
                description="a simple profile name",
            )
            sanitized_command.append(f"--profile={profile_name}")
            command_index += 1
            continue

        break

    delegated_command = command_tuple[command_index:]
    if delegated_command in ALLOWED_DELEGATED_DTS_COMMANDS:
        sanitized_command.extend(delegated_command)
        return tuple(sanitized_command)

    allowed_commands = [
        f"'{' '.join(command_parts)}'"
        for command_parts in ALLOWED_DELEGATED_DTS_COMMANDS
    ]
    allowed_description = ", ".join(allowed_commands)
    expected = (
        "a permitted delegated dts command "
        f"(optional global flags plus one of {allowed_description})"
    )
    raise RequestFieldValueError(REQUEST_COMMAND_FIELD, expected)


def delegated_command_key(command: tuple[str, ...]) -> tuple[str, ...]:
    command_index = 0
    while command_index < len(command):
        part = command[command_index]
        if not part.startswith("-"):
            break
        if part in ALLOWED_DTS_CLI_OPTIONS:
            command_index += 1
            continue
        if part == "--profile":
            command_index += 2
            continue
        if part.startswith("--profile=") and part != "--profile=":
            command_index += 1
            continue
        break
    return command[command_index:]


def validate_pattern_value(
    value: str,
    pattern: re.Pattern[str],
    *,
    option_name: str,
    description: str,
) -> str:
    if pattern.fullmatch(value) is None:
        raise invalid_option_value_error(option_name, description)
    return value


def validate_hostname_or_ip(value: str, *, option_name: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        stripped_value = value.rstrip(".")
        if not stripped_value:
            raise invalid_hostname_or_ip_error(option_name) from None
        labels = stripped_value.split(".")
        for label in labels:
            if not label:
                raise invalid_hostname_or_ip_error(option_name) from None
            if len(label) > HOST_LABEL_MAX_LENGTH:
                raise invalid_hostname_or_ip_error(option_name) from None
            if label.startswith("-") or label.endswith("-"):
                raise invalid_hostname_or_ip_error(option_name) from None
            if HOST_LABEL_PATTERN.fullmatch(label) is None:
                raise invalid_hostname_or_ip_error(option_name) from None
    return value


def validate_integer_option(
    value: str,
    *,
    option_name: str,
    minimum: int,
    maximum: int,
) -> str:
    try:
        integer_value = int(value)
    except ValueError as error:
        raise invalid_option_value_error(option_name, "an integer") from error

    if integer_value < minimum or integer_value > maximum:
        description = f"between {minimum} and {maximum}"
        raise invalid_option_value_error(option_name, description)
    return str(integer_value)


def read_option_value(
    argv: list[str],
    index: int,
    *,
    short_option: str | None = None,
    long_option: str,
) -> tuple[str | None, int]:
    arg = argv[index]
    if short_option is not None and arg == short_option:
        if index + 1 >= len(argv):
            raise missing_option_value_error(short_option)
        return argv[index + 1], 2
    if arg == long_option:
        if index + 1 >= len(argv):
            raise missing_option_value_error(long_option)
        return argv[index + 1], 2
    option_prefix = f"{long_option}="
    if arg.startswith(option_prefix):
        value = arg[len(option_prefix) :]
        if not value:
            raise missing_option_value_error(long_option)
        return value, 1
    return None, 0


def require_option_value(value: str | None, *, option_name: str) -> str:
    if value is None:
        raise missing_option_value_error(option_name)
    return value


def validate_renderer_binary_relative_parts(value: str) -> tuple[str, ...]:
    value_posix_path = PurePosixPath(value)
    sanitized_parts: list[str] = []

    for part in value_posix_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            message = (
                "--renderer-binary must stay within the delegated "
                "workspace."
            )
            raise delegated_argument_error(message)
        sanitized_parts.append(part)

    if not sanitized_parts:
        message = (
            "--renderer-binary must stay within the delegated workspace."
        )
        raise delegated_argument_error(message)

    return tuple(sanitized_parts)


def map_renderer_binary_path(
    value: str,
    *,
    host_cwd: str,
    container_root: str,
    host_root: str,
) -> str:
    if value.startswith("~"):
        message = "--renderer-binary must stay within the delegated workspace."
        raise delegated_argument_error(message)

    value_posix_path = PurePosixPath(value)
    if value_posix_path.is_absolute():
        return map_container_path(value, container_root, host_root)

    host_cwd_path = Path(host_cwd)
    host_root_path = Path(host_root)
    resolved_host_root_path = host_root_path.resolve(strict=True)
    relative_parts = validate_renderer_binary_relative_parts(value)
    candidate_path = host_cwd_path.joinpath(*relative_parts)
    try:
        resolved_candidate_path = candidate_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise HostMappedPathNotFoundError(value) from error
    ensure_path_is_within_host_root(
        value,
        resolved_candidate_path,
        resolved_host_root_path,
    )
    return str(resolved_candidate_path)


def sanitize_viewer_argv(
    command: tuple[str, ...],
    argv: list[str],
) -> list[str]:
    sanitized: list[str] = []
    robot_name: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in VIEWER_BOOLEAN_ARGS:
            sanitized.append(arg)
            index += 1
            continue
        if arg.startswith("-"):
            detail = f"unsupported option {arg!r}"
            raise invalid_delegated_arguments_error(command, detail)
        if robot_name is not None:
            detail = "expected a single robot hostname or IP address"
            raise invalid_delegated_arguments_error(command, detail)
        robot_name = validate_hostname_or_ip(arg, option_name="robot")
        sanitized.append(robot_name)
        index += 1

    if robot_name is None:
        detail = "missing robot hostname or IP address"
        raise invalid_delegated_arguments_error(command, detail)
    return sanitized


def sanitize_matrix_run_argv(  # noqa: C901, PLR0912, PLR0915
    command: tuple[str, ...],
    argv: list[str],
    *,
    host_cwd: str,
    container_root: str,
    host_root: str,
) -> list[str]:
    sanitized: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]

        value, consumed = read_option_value(
            argv,
            index,
            short_option="-v",
            long_option="--version",
        )
        if consumed:
            value = require_option_value(value, option_name="--version")
            version = validate_pattern_value(
                value,
                VERSION_PATTERN,
                option_name="--version",
                description="an alphanumeric version identifier",
            )
            sanitized.extend(["--version", version])
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            long_option="--renderer-binary",
        )
        if consumed:
            value = require_option_value(
                value,
                option_name="--renderer-binary",
            )
            renderer_binary = map_renderer_binary_path(
                value,
                host_cwd=host_cwd,
                container_root=container_root,
                host_root=host_root,
            )
            sanitized.extend(["--renderer-binary", renderer_binary])
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            short_option="-e",
            long_option="--engine",
        )
        if consumed:
            value = require_option_value(value, option_name="--engine")
            validate_hostname_or_ip(value, option_name="--engine")
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            short_option="-ep",
            long_option="--engine-control-port",
        )
        if consumed:
            value = require_option_value(
                value,
                option_name="--engine-control-port",
            )
            control_port = validate_integer_option(
                value,
                option_name="--engine-control-port",
                minimum=1,
                maximum=MAX_PORT_NUMBER,
            )
            sanitized.extend(["--engine-control-port", control_port])
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            short_option="-ewp",
            long_option="--engine-ws-control-port",
        )
        if consumed:
            value = require_option_value(
                value,
                option_name="--engine-ws-control-port",
            )
            websocket_port = validate_integer_option(
                value,
                option_name="--engine-ws-control-port",
                minimum=1,
                maximum=MAX_PORT_NUMBER,
            )
            sanitized.extend(["--engine-ws-control-port", websocket_port])
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            long_option="--port-offset",
        )
        if consumed:
            value = require_option_value(value, option_name="--port-offset")
            port_offset = validate_integer_option(
                value,
                option_name="--port-offset",
                minimum=0,
                maximum=MAX_PORT_NUMBER,
            )
            sanitized.extend(["--port-offset", port_offset])
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            short_option="-r",
            long_option="--renderer-id",
        )
        if consumed:
            value = require_option_value(value, option_name="--renderer-id")
            renderer_id = validate_integer_option(
                value,
                option_name="--renderer-id",
                minimum=0,
                maximum=MAX_RENDERER_ID,
            )
            sanitized.extend(["--renderer-id", renderer_id])
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            short_option="-k",
            long_option="--renderer-key",
        )
        if consumed:
            value = require_option_value(value, option_name="--renderer-key")
            renderer_key = validate_pattern_value(
                value,
                SAFE_TOKEN_PATTERN,
                option_name="--renderer-key",
                description="a token-like renderer key",
            )
            sanitized.extend(["--renderer-key", renderer_key])
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            short_option="-os",
            long_option="--os-family",
        )
        if consumed:
            if value not in OS_FAMILY_VALUES:
                option_name = "--os-family"
                raise option_must_be_one_of_error(
                    option_name,
                    OS_FAMILY_VALUES,
                )
            sanitized.extend(["--os-family", value])
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            long_option="--engine-name",
        )
        if consumed:
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            short_option="-m",
            long_option="--map",
        )
        if consumed:
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            long_option="--host",
        )
        if consumed:
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            long_option="--port",
        )
        if consumed:
            index += consumed
            continue

        value, consumed = read_option_value(
            argv,
            index,
            long_option="--xvfb-args",
        )
        if consumed:
            option_name = "--xvfb-args"
            raise option_not_permitted_error(option_name)

        value, consumed = read_option_value(
            argv,
            index,
            long_option="--container-image",
        )
        if consumed:
            option_name = "--container-image"
            raise option_not_permitted_error(option_name)

        if arg == "--link":
            if index + 2 >= len(argv):
                message = "--link requires exactly two values."
                raise delegated_argument_error(message)
            index += 3
            continue

        if arg in MATRIX_BOOLEAN_ARG_MAP:
            sanitized.append(MATRIX_BOOLEAN_ARG_MAP[arg])
            index += 1
            continue

        if arg in MATRIX_IGNORED_FLAGS:
            index += 1
            continue

        if arg in {"--browser", "--local", "--xvfb"}:
            raise option_not_permitted_error(arg)

        if arg.startswith("-"):
            detail = f"unsupported option {arg!r}"
            raise invalid_delegated_arguments_error(command, detail)
        detail = f"unexpected positional argument {arg!r}"
        raise invalid_delegated_arguments_error(command, detail)

    return sanitized


def sanitize_delegated_command_argv(
    command: tuple[str, ...],
    argv: list[str],
    *,
    host_cwd: str,
    container_root: str,
    host_root: str,
) -> list[str]:
    command_key = delegated_command_key(command)
    if command_key == ("matrix", "run"):
        return sanitize_matrix_run_argv(
            command,
            argv,
            host_cwd=host_cwd,
            container_root=container_root,
            host_root=host_root,
        )
    return sanitize_viewer_argv(command, argv)


def parse_request_payload(
    payload: object,
    config: argparse.Namespace,
) -> tuple[list[str], str, dict[str, str], bool]:
    if not isinstance(payload, dict):
        raise RequestFieldTypeError(REQUEST_ENV_FIELD, REQUEST_ENV_EXPECTATION)

    command = payload.get("command")
    argv = payload.get("argv", [])
    cwd = payload.get("cwd")
    forwarded_env = payload.get("env", {})

    if not isinstance(argv, list):
        raise RequestFieldTypeError(
            REQUEST_ARGV_FIELD,
            REQUEST_ARGV_EXPECTATION,
        )
    if not all(isinstance(arg, str) for arg in argv):
        raise RequestFieldTypeError(
            REQUEST_ARGV_FIELD,
            REQUEST_ARGV_EXPECTATION,
        )
    if not isinstance(cwd, str):
        raise RequestFieldTypeError(REQUEST_CWD_FIELD, REQUEST_CWD_EXPECTATION)
    if not cwd:
        raise RequestFieldValueError(
            REQUEST_CWD_FIELD,
            REQUEST_CWD_EXPECTATION,
        )
    if not isinstance(forwarded_env, dict):
        raise RequestFieldTypeError(REQUEST_ENV_FIELD, REQUEST_ENV_EXPECTATION)

    command_path = validate_delegated_command_path(command)
    host_cwd = map_container_path(cwd, config.container_root, config.host_root)
    host_cwd_path = Path(host_cwd)
    if not host_cwd_path.is_dir():
        raise HostWorkingDirectoryError(host_cwd)

    child_env = build_child_environment(forwarded_env)
    mapped_argv = sanitize_delegated_command_argv(
        command_path,
        argv,
        host_cwd=host_cwd,
        container_root=config.container_root,
        host_root=config.host_root,
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
        with open_chunk_file_for_write(chunk_path) as stream:
            stream.write(self._pending)
            stream.flush()
            file_descriptor = stream.fileno()
            os.fsync(file_descriptor)
        self._chunk_index += 1
        self._pending.clear()
        directory_fd: int | None = None
        try:
            nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(
                self._stream_path.parent,
                os.O_RDONLY | nofollow_flag,
            )
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def close(self) -> None:
        self.flush()


def validate_request_id(request_id: str) -> str:
    if REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        raise invalid_request_id_error(request_id)
    return request_id


def ensure_request_artifact_is_not_symlink(request_path: Path) -> None:
    if request_path.is_symlink():
        raise symlinked_request_artifact_error(request_path.name)


def request_path_for_suffix(
    requests_dir: Path,
    request_id: str,
    suffix: str,
) -> Path:
    validated_request_id = validate_request_id(request_id)
    return requests_dir / f"{validated_request_id}{suffix}"


def open_chunk_file_for_write(chunk_path: Path) -> BinaryIO:
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(chunk_path, flags | nofollow_flag, 0o600)
    return os.fdopen(file_descriptor, "wb")


def read_request_payload_text(request_path: Path) -> str:
    flags = os.O_RDONLY
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(request_path, flags | nofollow_flag)
    with os.fdopen(file_descriptor, "r", encoding="utf-8") as request_stream:
        return request_stream.read()


def cancel_request_is_signaled(cancel_path: Path) -> bool:
    try:
        cancel_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return False

    return not cancel_path.is_symlink()


def request_heartbeat_is_stale(
    heartbeat_path: Path,
    *,
    stale_after_seconds: float = REQUEST_HEARTBEAT_STALE_SECONDS,
) -> bool:
    try:
        heartbeat_stat = heartbeat_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return True

    if heartbeat_path.is_symlink():
        return True

    heartbeat_mtime = heartbeat_stat.st_mtime
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
        if cancel_request_is_signaled(cancel_path):
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
    request_id: str,
    requests_dir: Path,
    config: argparse.Namespace,
) -> None:
    processing_path = request_path_for_suffix(
        requests_dir,
        request_id,
        PROCESSING_FILE_SUFFIX,
    )
    stream_path = request_path_for_suffix(
        requests_dir,
        request_id,
        STREAM_FILE_SUFFIX,
    )
    cancel_path = request_path_for_suffix(
        requests_dir,
        request_id,
        CANCEL_FILE_SUFFIX,
    )
    heartbeat_path = request_path_for_suffix(
        requests_dir,
        request_id,
        HEARTBEAT_FILE_SUFFIX,
    )

    writer = FileStreamWriter(stream_path)
    try:
        payload_text = read_request_payload_text(processing_path)
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
        write_encoded(writer, exit_code_message)
        writer.flush()
    finally:
        writer.close()
        processing_path.unlink(missing_ok=True)
        cancel_path.unlink(missing_ok=True)
        heartbeat_path.unlink(missing_ok=True)


def start_request_processor(
    request_id: str,
    requests_dir: Path,
    config: argparse.Namespace,
) -> None:
    worker_id = uuid.uuid4()
    worker_suffix = worker_id.hex[:8]
    worker_name = f"host-runner-request-{worker_suffix}"
    request_thread = threading.Thread(
        target=process_request_file,
        args=(request_id, requests_dir, config),
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
            request_paths = sorted(
                candidate.name for candidate in requests_dir.iterdir()
            )
            for request_name in request_paths:
                if not request_name.endswith(REQUEST_FILE_SUFFIX):
                    continue
                try:
                    request_id = validate_request_id(
                        request_name[: -len(REQUEST_FILE_SUFFIX)],
                    )
                except RequestQueuePathError as error:
                    message = f"Host runner rejected request artifact: {error}"
                    write_line(sys.stderr, message)
                    continue

                request_path = request_path_for_suffix(
                    requests_dir,
                    request_id,
                    REQUEST_FILE_SUFFIX,
                )
                ensure_request_artifact_is_not_symlink(request_path)
                claimed_path = request_path_for_suffix(
                    requests_dir,
                    request_id,
                    PROCESSING_FILE_SUFFIX,
                )
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
                start_request_processor(request_id, requests_dir, config)
        except Exception as error:  # noqa: BLE001
            failure_message = (
                f"Host runner request watcher failed: {error}"
            )
            write_line(sys.stderr, failure_message)

        status.heartbeat()
        time.sleep(0.1)


def stream_process_output(  # noqa: C901, PLR0913
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
        exec_message = "[host-runner] exec: delegated host-side process\n"
        write_encoded(wfile, exec_message)
        wfile.flush()

    if cancel_path is not None and cancel_request_is_signaled(cancel_path):
        exit_code_message = f"{HOST_RUNNER_EXIT_CODE_PREFIX}130\n"
        write_encoded(wfile, exit_code_message)
        wfile.flush()
        return
    if heartbeat_path is not None and request_heartbeat_is_stale(
        heartbeat_path,
    ):
        exit_code_message = f"{HOST_RUNNER_EXIT_CODE_PREFIX}130\n"
        write_encoded(wfile, exit_code_message)
        wfile.flush()
        return

    try:
        process, stdout_stream = start_streamed_process(command_list, cwd, env)
    except OSError as error:
        launch_error_message = f"Failed to launch host-side process: {error}\n"
        write_encoded(wfile, launch_error_message)
        exit_code_message = f"{HOST_RUNNER_EXIT_CODE_PREFIX}127\n"
        write_encoded(wfile, exit_code_message)
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
    write_encoded(wfile, exit_code_message)
    wfile.flush()


def make_handler(
    config: argparse.Namespace,
) -> type[BaseHTTPRequestHandler]:
    class HostRunnerHandler(BaseHTTPRequestHandler):

        def do_GET(self) -> None:
            request_path = self.path.rstrip("/")
            if request_path == "/healthz":
                request_watcher_healthy, health_message = (
                    request_watcher_ready(config)
                )
                if not request_watcher_healthy:
                    self.send_response(503)
                    self.send_header(
                        "Content-Type",
                        "text/plain; charset=utf-8",
                    )
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
            request_path = self.path.rstrip("/")
            if request_path != "/run":
                self.send_error(404, "Not found")
                return

            if config.token is not None:
                expected = f"Bearer {config.token}"
                authorization_header = self.headers.get("Authorization")
                if authorization_header != expected:
                    self.send_error(403, "Forbidden")
                    return

            try:
                content_length_value = self.headers.get("Content-Length", "0")
                content_length = int(content_length_value)
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
                error_message = str(error)
                self.send_error(400, error_message)
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
            write_line(
                sys.stderr,
                "Host runner request watcher stopped; restarting.",
            )
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
    host_root_path = Path(args.host_root).expanduser()
    if not host_root_path.is_dir():
        raise HostRootNotFoundError(args.host_root)
    host_root_path = host_root_path.resolve(strict=True)
    args.host_root = str(host_root_path)

    if args.requests_dir is not None:
        ensure_default_requests_dir(args.requests_dir)
        DEFAULT_REQUESTS_DIR_PATH.mkdir(parents=True, exist_ok=True)
        requests_dir_path = DEFAULT_REQUESTS_DIR_PATH.resolve(strict=True)
        args.requests_dir = str(requests_dir_path)

    is_loopback_listener = args.listen_host in LOOPBACK_LISTEN_HOSTS
    if args.token is None and not is_loopback_listener:
        raise ListenerConfigurationError

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
    exit_code = main()
    raise SystemExit(exit_code)
