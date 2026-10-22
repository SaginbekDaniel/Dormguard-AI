"""Synthetic inventory fixtures only; these are not DormGuard application tests."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "check_source_readiness.py"
spec = importlib.util.spec_from_file_location("source_readiness", SCRIPT)
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


class SourceReadinessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fill_inventory(self):
        for name in readiness.REQUIRED_FILES:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic inventory fixture, not application code\n")

    def test_documentation_only_transfer_is_blocked(self):
        (self.root / "HANDOFF.md").write_text("historical claims")
        report = readiness.inspect_source(self.root)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(len(report["problems"]), len(readiness.REQUIRED_FILES))

    def test_complete_inventory_passes_presence_only(self):
        self.fill_inventory()
        report = readiness.inspect_source(self.root)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["problems"], [])
        self.assertIn("File presence only", report["scope"])

    def test_empty_and_directory_entries_are_blocked(self):
        self.fill_inventory()
        (self.root / "requirements.txt").write_text("")
        (self.root / "README.md").unlink()
        (self.root / "README.md").mkdir()
        problems = {p["path"]: p["reason"] for p in readiness.inspect_source(self.root)["problems"]}
        self.assertEqual(problems, {"README.md": "missing_or_not_file", "requirements.txt": "empty"})

    def test_symlink_escape_is_blocked_without_reading_target(self):
        self.fill_inventory()
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "private"
            target.write_text("PRIVATE_TEST_VALUE")
            (self.root / "README.md").unlink()
            (self.root / "README.md").symlink_to(target)
            report = readiness.inspect_source(self.root)
            self.assertEqual(report["problems"], [{"path": "README.md", "reason": "outside_root"}])
            self.assertNotIn("PRIVATE_TEST_VALUE", json.dumps(report))

    def test_cli_missing_root_has_nonzero_exit_and_json(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root / "absent")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "BLOCKED")
        self.assertEqual(result.stderr, "")

    def test_cli_success_never_imports_application(self):
        self.fill_inventory()
        (self.root / "dormguard/app.py").write_text("raise RuntimeError('must never execute')\n")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
