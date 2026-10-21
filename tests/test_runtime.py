import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from miexiang_publish import paths
from miexiang_publish.cli import main
from miexiang_publish.errors import PublishError
from miexiang_publish.models import identify_artifact
from miexiang_publish.staging import stage_snapshot
from miexiang_publish.offline import run_demo
from miexiang_publish.credentials import CosCredentials
from miexiang_publish.profile import CosProfile
from miexiang_publish.providers.tencent_cos import _default_client_factory


class RuntimeTests(unittest.TestCase):
    def tearDown(self):
        paths.configure_runtime()

    def test_cli_override_unicode_and_spaces_without_profile_or_network(self):
        with tempfile.TemporaryDirectory(prefix="runtime-") as tmp:
            root = Path(tmp) / "数据 with spaces"
            with patch("miexiang_publish.cli._load_optional_profile", side_effect=AssertionError("offline read profile")):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["--data-dir", str(root), "offline-demo"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(paths.RUNTIME_ROOT, root)
            self.assertEqual(result["PUBLISH_STATUS"], "OFFLINE_FIXTURE_PASS")
            self.assertEqual(result["REMOTE_VERIFICATION"], "NOT_RUN")
            self.assertFalse(root.exists())

    def test_snapshot_and_lock_use_custom_directory_and_preserve_source(self):
        from miexiang_publish.locking import IdentityLock
        with tempfile.TemporaryDirectory(prefix="runtime-") as tmp:
            root = Path(tmp)
            source = root / "原始 file.bin"
            source.write_bytes(b"source immutable")
            paths.configure_runtime(root / "运行 data")
            artifact = identify_artifact(source, "fixture", "1.0", "test")
            staged = stage_snapshot(artifact)
            self.assertTrue(staged.source_path.is_relative_to(paths.RUNTIME_ROOT))
            self.assertEqual(source.read_bytes(), staged.source_path.read_bytes())
            with IdentityLock("fixture", "test", "1.0"):
                with self.assertRaises(PublishError) as caught:
                    IdentityLock("fixture", "test", "1.0").acquire()
                self.assertEqual(caught.exception.code, "LOCK_HELD")
            self.assertEqual(list(paths.LOCK_ROOT.glob("*.lock")), [])

    def test_environment_default_and_explicit_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MIEXIANG_PUBLISH_DATA_DIR": str(Path(tmp) / "环境 data")}):
                paths.configure_runtime()
                self.assertEqual(paths.RUNTIME_ROOT, Path(tmp) / "环境 data")
                paths.configure_runtime(Path(tmp) / "explicit")
                self.assertEqual(paths.RUNTIME_ROOT, Path(tmp) / "explicit")

    def test_default_platform_branches_do_not_use_package_directory(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=Path("synthetic-home")):
            for platform, suffix in (("win32", "AppData/Local/miexiang-publish"), ("darwin", "Library/Application Support/miexiang-publish"), ("linux", ".local/share/miexiang-publish")):
                with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                    self.assertEqual(paths.default_data_root(), Path("synthetic-home") / suffix)

    def test_unwritable_root_and_link_errors_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths.configure_runtime(Path(tmp) / "private-root")
            with patch.object(Path, "mkdir", side_effect=PermissionError("secret local detail")):
                with self.assertRaises(PublishError) as caught:
                    paths.ensure_runtime_roots()
                self.assertEqual(caught.exception.code, "POLICY_REJECTED")
                self.assertNotIn("secret", caught.exception.message)
            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(PublishError):
                    paths.ensure_runtime_roots()

    def test_processes_contend_on_same_identity_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, MIEXIANG_PUBLISH_DATA_DIR=tmp)
            code = "from miexiang_publish.locking import IdentityLock; l=IdentityLock('fixture','test','1.0').acquire(); print('HELD',flush=True); input(); l.release()"
            process = subprocess.Popen([sys.executable, "-c", code], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "HELD")
                contender = subprocess.run([sys.executable, "-c", "from miexiang_publish.locking import IdentityLock; from miexiang_publish.errors import PublishError;\ntry: IdentityLock('fixture','test','1.0').acquire()\nexcept PublishError as e: print(e.code)"], env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(contender.returncode, 0, contender.stderr)
                self.assertEqual(contender.stdout.strip(), "LOCK_HELD")
            finally:
                process.communicate("release\n", timeout=20)
            retry = subprocess.run([sys.executable, "-c", "from miexiang_publish.locking import IdentityLock;\nwith IdentityLock('fixture','test','1.0'): print('ACQUIRED')"], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(retry.stdout.strip(), "ACQUIRED", retry.stderr)

    @unittest.skipIf(importlib.util.find_spec("qcloud_cos"), "run absent-extra check in GitHub-only job")
    def test_missing_cos_dependency_has_actionable_bounded_error(self):
        profile = CosProfile("fixture-1234567890", "ap-beijing", "fixture", False, False, False, False, {}, 1000)
        with self.assertRaises(PublishError) as caught:
            _default_client_factory(profile, CosCredentials("synthetic-id", "synthetic-key"))
        self.assertIn("[cos]", caught.exception.message)
        self.assertNotIn("synthetic-key", caught.exception.message)

    def test_demo_checks_include_negative_cases(self):
        result = run_demo()
        self.assertTrue({"truncated", "oversized", "wrong_hash", "different_bytes_conflict"}.issubset(result["CHECKS"]))
        self.assertFalse(result["REMOTE_WRITE_PERFORMED"])


class SafetyRegressionTests(unittest.TestCase):
    def tearDown(self):
        paths.configure_runtime()

    def test_optimized_demo_does_not_drop_manifest_check(self):
        code = "from unittest.mock import patch; from miexiang_publish.offline import run_demo;\nwith patch('miexiang_publish.offline.same_release_identity', return_value=False): run_demo()"
        result = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("observation time changed", result.stderr)
        self.assertNotIn("OFFLINE_FIXTURE_PASS", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_report_junction_cannot_escape_runtime_root(self):
        import _winapi
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            outside = root / "outside"
            outside.mkdir()
            paths.configure_runtime(data)
            paths.ensure_runtime_roots()
            link = data / "reports"
            _winapi.CreateJunction(str(outside), str(link))
            try:
                with self.assertRaises(PublishError) as caught:
                    paths.ensure_runtime_directory(link / "fixture" / "test" / "1.0")
                self.assertEqual(caught.exception.code, "POLICY_REJECTED")
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                os.rmdir(link)
