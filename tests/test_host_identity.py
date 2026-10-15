import contextlib
import importlib.util
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".devcontainer/scripts/host_runner_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("workspace_bootstrap", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load Workspace bootstrap from {SCRIPT}")
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)

MACHINE_UUID = "12345678-1234-5678-1234-567812345678"
MACHINE_ID = MACHINE_UUID.replace("-", "")
OTHER_MACHINE_ID = "87654321876543218765432187654321"


class HostIdentityTests(unittest.TestCase):
    def test_linux_identity_is_normalized(self):
        with (
            patch.object(bootstrap.sys, "platform", "linux"),
            patch.object(
                bootstrap.Path,
                "read_text",
                return_value=MACHINE_ID.upper() + "\n",
            ),
        ):
            self.assertEqual(MACHINE_ID, bootstrap._read_host_machine_id())

    def test_linux_dbus_fallback_is_only_used_for_an_absent_file(self):
        with (
            patch.object(bootstrap.sys, "platform", "linux"),
            patch.object(
                bootstrap.Path,
                "read_text",
                side_effect=[FileNotFoundError(), MACHINE_ID],
            ) as reader,
        ):
            self.assertEqual(MACHINE_ID, bootstrap._read_host_machine_id())
            self.assertEqual(2, reader.call_count)
        with (
            patch.object(bootstrap.sys, "platform", "linux"),
            patch.object(
                bootstrap.Path,
                "read_text",
                side_effect=PermissionError("denied"),
            ) as reader,
        ):
            with self.assertRaises(PermissionError):
                bootstrap._read_host_machine_id()
            self.assertEqual(1, reader.call_count)

    def test_invalid_or_uninitialized_identity_is_rejected(self):
        for value in ("", "not-a-machine-id", "0" * 32):
            with (
                self.subTest(value=value),
                patch.object(bootstrap.sys, "platform", "linux"),
                patch.object(bootstrap.Path, "read_text", return_value=value),
                self.assertRaises(ValueError),
            ):
                bootstrap._read_host_machine_id()

    def test_macos_identity_matches_the_native_platform_uuid(self):
        with (
            patch.object(bootstrap.sys, "platform", "darwin"),
            patch.object(
                bootstrap.subprocess,
                "check_output",
                return_value=plistlib.dumps(
                    [{"IOPlatformUUID": MACHINE_UUID.upper()}]
                ),
            ) as command,
        ):
            self.assertEqual(MACHINE_ID, bootstrap._read_host_machine_id())
        self.assertEqual(5, command.call_args.kwargs["timeout"])
        self.assertEqual("/usr/sbin/ioreg", command.call_args.args[0][0])

    def test_macos_requires_a_valid_uuid_in_the_expected_plist(self):
        for value in ([], [{}], {"IOPlatformUUID": MACHINE_UUID}):
            with (
                self.subTest(value=value),
                patch.object(bootstrap.sys, "platform", "darwin"),
                patch.object(
                    bootstrap.subprocess,
                    "check_output",
                    return_value=plistlib.dumps(value),
                ),
                self.assertRaises((TypeError, ValueError)),
            ):
                bootstrap._read_host_machine_id()

    def test_windows_reads_the_native_machine_registry_view(self):
        registry = SimpleNamespace(
            HKEY_LOCAL_MACHINE=0,
            KEY_READ=1,
            KEY_WOW64_64KEY=2,
            OpenKey=MagicMock(),
            QueryValueEx=MagicMock(return_value=(MACHINE_UUID, 1)),
        )
        with (
            patch.object(bootstrap.sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": registry}),
        ):
            self.assertEqual(MACHINE_ID, bootstrap._read_host_machine_id())
        registry.OpenKey.assert_called_once_with(
            registry.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            registry.KEY_READ | registry.KEY_WOW64_64KEY,
        )

    def test_unsupported_host_platform_is_rejected(self):
        with (
            patch.object(bootstrap.sys, "platform", "unsupported"),
            self.assertRaisesRegex(ValueError, "not supported"),
        ):
            bootstrap._read_host_machine_id()


class HostIdentityFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / ".host-machine-id"
        reader = patch.object(
            bootstrap, "_read_host_machine_id", return_value=MACHINE_ID
        )
        reader.start()
        self.addCleanup(reader.stop)

    def test_identity_is_written_in_linux_machine_id_format(self):
        self.assertEqual(0, bootstrap._write_host_machine_id(self.output))
        self.assertEqual(MACHINE_ID + "\n", self.output.read_text())
        if os.name != "nt":
            self.assertEqual(0o644, stat.S_IMODE(self.output.stat().st_mode))

    def test_repeated_initialization_preserves_the_mounted_inode(self):
        bootstrap._write_host_machine_id(self.output)
        original = self.output.stat()
        bootstrap._write_host_machine_id(self.output)
        current = self.output.stat()
        self.assertEqual(original.st_ino, current.st_ino)
        self.assertEqual(original.st_mtime_ns, current.st_mtime_ns)

    def test_copied_or_stale_identity_is_replaced_with_the_current_host(self):
        self.output.write_text(OTHER_MACHINE_ID + "\n")
        bootstrap._write_host_machine_id(self.output)
        self.assertEqual(MACHINE_ID + "\n", self.output.read_text())

    def test_corrupt_generated_content_is_repaired(self):
        self.output.write_bytes(b"\xffinvalid")
        bootstrap._write_host_machine_id(self.output)
        self.assertEqual(MACHINE_ID + "\n", self.output.read_text())

    def test_symlink_and_directory_outputs_are_rejected(self):
        target = self.output.parent / "unrelated"
        target.write_text("do not modify")
        self.output.symlink_to(target)
        with self.assertRaises(ValueError):
            bootstrap._write_host_machine_id(self.output)
        self.assertEqual("do not modify", target.read_text())
        self.output.unlink()
        self.output.mkdir()
        with self.assertRaises(ValueError):
            bootstrap._write_host_machine_id(self.output)

    def test_failed_replacement_preserves_the_existing_file_and_cleans_up(
        self,
    ):
        self.output.write_text(OTHER_MACHINE_ID + "\n")
        with (
            patch.object(
                bootstrap.Path,
                "replace",
                side_effect=OSError("cannot replace"),
            ),
            self.assertRaises(OSError),
        ):
            bootstrap._write_host_machine_id(self.output)
        self.assertEqual(OTHER_MACHINE_ID + "\n", self.output.read_text())
        self.assertEqual([self.output], list(self.output.parent.iterdir()))

    def test_cli_writes_identity_without_printing_the_raw_uuid(self):
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "bootstrap",
                    "write-machine-id",
                    "--output",
                    str(self.output),
                ],
            ),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(0, bootstrap.main())
        self.assertEqual(MACHINE_ID + "\n", self.output.read_text())
        self.assertEqual("", stdout.getvalue())

    def test_cli_reports_identity_failure_and_does_not_create_a_file(self):
        stderr = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "bootstrap",
                    "write-machine-id",
                    "--output",
                    str(self.output),
                ],
            ),
            patch.object(
                bootstrap,
                "_read_host_machine_id",
                side_effect=ValueError("uninitialized machine identity"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(1, bootstrap.main())
        self.assertIn(
            "Could not prepare the host machine identity", stderr.getvalue()
        )
        self.assertFalse(self.output.exists())


class WorkspaceIdentityMountTests(unittest.TestCase):
    def test_compose_mounts_both_identity_paths_read_only_without_fallback(
        self,
    ):
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("Docker Compose is required for mount validation.")
        environment = os.environ.copy()
        environment.update(
            WORKSPACE_IMAGE_TAG="identity-check",
            DTS_HOST_RUNNER_URL="http://127.0.0.1:59321/run",
            DTS_HOST_RUNNER_TOKEN="identity-check",
        )
        result = subprocess.run(
            [
                docker,
                "compose",
                "--env-file",
                os.devnull,
                "-f",
                str(ROOT / ".devcontainer/docker-compose.yml"),
                "config",
                "--format",
                "json",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        configuration = json.loads(result.stdout)["services"]["workspace"]
        targets = {"/etc/machine-id", "/var/lib/dbus/machine-id"}
        mounts = [
            volume
            for volume in configuration["volumes"]
            if volume["target"] in targets
        ]
        self.assertEqual(targets, {mount["target"] for mount in mounts})
        for mount in mounts:
            self.assertEqual("bind", mount["type"])
            self.assertEqual(
                ROOT / ".devcontainer/.host-machine-id",
                Path(mount["source"]),
            )
            self.assertTrue(mount["read_only"])
            # Compose omits false-valued bind options in normalized JSON.
            self.assertIsNot(mount["bind"].get("create_host_path"), True)


if __name__ == "__main__":
    unittest.main()
