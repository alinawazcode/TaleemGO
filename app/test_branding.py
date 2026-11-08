"""
test_branding.py - Tests enforcing TaleemGo AI branding and deprecation rules.
"""
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "app"))


class BrandingTests(unittest.TestCase):

    def test_no_unapproved_saathi_references(self):
        """Scan all tracked files for saathi/sathi and ensure only allowed references exist."""
        # Get all tracked files using git ls-files
        cmd = ["git", "ls-files"]
        res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=True)
        tracked_files = res.stdout.strip().splitlines()

        pattern = re.compile(r"saathi|sathi", re.IGNORECASE)

        # Allow-list criteria:
        # 1. This test file itself
        # 2. Repository URLs (e.g. github.com/.../Saathi.git, cd Saathi)
        # 3. SATHI_SECRET_KEY fallback lines (in lms_client.py, .env.example, README.md, test files)
        # 4. /*saathi-patch*/ marker in patch-wwebjs.js
        # 5. Negative test asserting that "saathi ai" no longer activates
        allow_rules = [
            lambda path, line: "test_branding.py" in path,
            lambda path, line: "github.com" in line or "cd Saathi" in line or "clone" in line,
            lambda path, line: "SATHI_SECRET_KEY" in line,
            lambda path, line: "saathi-patch" in line,
            lambda path, line: "saathi ai" in line.lower() and ("no longer activates" in line or "wa_bot.handle" in line),
        ]

        violations = []
        for rel_path in tracked_files:
            # Skip binary files or lock files if any
            if "package-lock.json" in rel_path and "node_modules" in rel_path:
                continue
            full_path = ROOT / rel_path
            if not full_path.is_file():
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, 1):
                        if pattern.search(line):
                            allowed = any(rule(rel_path, line) for rule in allow_rules)
                            if not allowed:
                                violations.append(f"{rel_path}:{line_no}: {line.strip()}")
            except Exception as e:
                self.fail(f"Failed to read {rel_path}: {e}")

        self.assertEqual(violations, [], "Found unapproved saathi/sathi occurrences:\n" + "\n".join(violations))

    def test_default_activation_phrases(self):
        """Assert default activation phrases match 'Hi TaleemGo AI', 'taleem go ai', 'TALEEMGO', and reject 'saathi ai'."""
        import wa_bot
        phrases = ["taleemgo ai", "taleem go ai", "taleemgo"]
        self.assertTrue(wa_bot.contains_activation_phrase("Hi TaleemGo AI", phrases))
        self.assertTrue(wa_bot.contains_activation_phrase("taleem go ai", phrases))
        self.assertTrue(wa_bot.contains_activation_phrase("hello TALEEMGO", phrases))
        self.assertTrue(wa_bot.contains_activation_phrase("TALEEMGO", phrases))
        self.assertFalse(wa_bot.contains_activation_phrase("Hi saathi ai", phrases))
        self.assertFalse(wa_bot.contains_activation_phrase("sathi ai", phrases))

    def test_secret_key_fallback(self):
        """Assert TALEEMGO_SECRET_KEY fallback to SATHI_SECRET_KEY works."""
        import lms_client
        from cryptography.fernet import Fernet

        fake_key = Fernet.generate_key().decode()

        # Case 1: TALEEMGO_SECRET_KEY is set
        with patch.dict(os.environ, {"TALEEMGO_SECRET_KEY": fake_key, "SATHI_SECRET_KEY": ""}):
            f1 = lms_client._fernet()
            self.assertIsNotNone(f1)

        # Case 2: Only deprecated SATHI_SECRET_KEY is set -> falls back successfully
        with patch.dict(os.environ, {"TALEEMGO_SECRET_KEY": "", "SATHI_SECRET_KEY": fake_key}):
            f2 = lms_client._fernet()
            self.assertIsNotNone(f2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
