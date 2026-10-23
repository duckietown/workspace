import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / ".devcontainer/scripts/host_runner.py"
SPEC = importlib.util.spec_from_file_location("workspace_viewer_host_runner", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load Workspace host runner from {SCRIPT}")
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class ViewerHandoffTests(unittest.TestCase):
    def test_request_maps_a_relative_handoff_to_the_native_shared_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "native-workspace"
            workspace.mkdir()
            requests = root / "native-queue"
            private = requests / "viewer-session"
            private.mkdir(parents=True)
            handoff = private / "desktop.json"
            handoff.write_text("{}", encoding="utf-8")
            handoff.chmod(0o600)
            config = SimpleNamespace(
                container_root="/workspaces/project", host_root=str(workspace),
                requests_dir=str(requests), dts_executable="dts",
            )
            payload = {
                "command": ["duckiebot", "image_viewer"],
                "argv": ["testbot"], "cwd": "/workspaces/project",
                "env": {"HOST_DTS_VIEWER_HANDOFF": "viewer-session/desktop.json"},
            }
            _, cwd, environment, _ = runner.parse_request_payload(payload, config)
            self.assertEqual(str(workspace), cwd)
            self.assertEqual(str(handoff), environment["DTS_VIEWER_HANDOFF"])
            self.assertEqual("viewer-session/desktop.json", payload["env"]["HOST_DTS_VIEWER_HANDOFF"])

    def test_absolute_traversing_ambiguous_or_non_desktop_handoffs_are_rejected(self):
        for value in (
            "/tmp/viewer/desktop.json", "../desktop.json",
            "viewer/../desktop.json", "viewer//desktop.json", "viewer/./desktop.json",
            "C:/viewer/desktop.json", "viewer\\desktop.json",
            "viewer/backend.json", None,
        ):
            with self.subTest(value=value), self.assertRaises((runner.RequestFieldTypeError, runner.RequestFieldValueError)):
                runner.map_viewer_handoff(value, "/not-used")

    @unittest.skipIf(os.name == "nt", "Creating Windows symlinks requires additional privileges.")
    def test_symlink_escapes_and_symlinked_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "shared"
            shared.mkdir()
            outside = root / "outside"
            outside.mkdir()
            handoff = outside / "desktop.json"
            handoff.write_text("{}", encoding="utf-8")
            handoff.chmod(0o600)
            (shared / "escaped").symlink_to(outside, target_is_directory=True)
            private = shared / "private"
            private.mkdir()
            (private / "desktop.json").symlink_to(handoff)
            for value in ("escaped/desktop.json", "private/desktop.json"):
                with self.subTest(value=value), self.assertRaises(runner.RequestFieldValueError):
                    runner.map_viewer_handoff(value, str(shared))
