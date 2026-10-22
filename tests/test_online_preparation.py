"""Private volume provisioning must refuse unsafe targets before touching data."""

import tempfile
import unittest
from pathlib import Path

from scripts.prepare_online_demo import prepare


class OnlinePreparationTests(unittest.TestCase):
    def test_requires_scope_and_preserves_existing_directory(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            original = root / "retained.txt"
            original.write_text("preserve")
            for consent in (False, True):
                with self.subTest(consent=consent), self.assertRaises(ValueError):
                    prepare(root, "https://demo.example.invalid", root / "model", root / "assets", consent)
            self.assertEqual(original.read_text(), "preserve")
            self.assertEqual(list(root.iterdir()), [original])

    def test_insecure_origin_fails_before_model_or_database_provisioning(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "new-data"
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                prepare(target, "http://127.0.0.1:8000", target / "model", target / "assets", True)
            self.assertFalse(target.exists())
