"""Prevent accidental publication of local tool-session caches."""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scan_source_secrets.py"
spec = importlib.util.spec_from_file_location("secret_scan", SCRIPT)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class SecretScanTests(unittest.TestCase):
    def test_staged_tool_session_cache_is_rejected_without_printing_contents(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            previous = Path.cwd()
            try:
                os.chdir(root)
                for name in (".impeccable/hook.cache.json", "docs/engineering/ONLINE_RUNTIME_RESULTS.json"):
                    with self.subTest(path=name):
                        cache = root / name
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        cache.write_text('{"sessions": ["PRIVATE_CONTROLLED_FIXTURE"]}')
                        output = io.StringIO()
                        with patch("sys.argv", [str(SCRIPT), "--staged"]), patch.object(
                            scanner.subprocess, "check_output", return_value=name.encode() + b"\0"
                        ), contextlib.redirect_stdout(output):
                            self.assertEqual(scanner.main(), 1)
                        report = json.loads(output.getvalue())
                        self.assertEqual(report["findings"][0]["rule"], "runtime_or_private_asset")
                        self.assertNotIn("PRIVATE_CONTROLLED_FIXTURE", output.getvalue())
            finally:
                os.chdir(previous)
