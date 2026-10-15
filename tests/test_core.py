from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from faultline.core import scan_path


class ScanPathTests(unittest.TestCase):
    def test_finds_markers_with_line_numbers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            path.write_text("print(1)\n# TODO: handle input\n", encoding="utf-8")

            findings = scan_path(path)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 2)
        self.assertEqual(findings[0].marker, "TODO")

    def test_skips_virtual_environment_directories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".venv").mkdir()
            (root / ".venv" / "ignored.py").write_text("# BUG", encoding="utf-8")
            (root / "kept.py").write_text("# FIXME", encoding="utf-8")

            findings = scan_path(root)

        self.assertEqual([finding.marker for finding in findings], ["FIXME"])

    def test_missing_path_is_reported_to_caller(self):
        with self.assertRaises(FileNotFoundError):
            scan_path("does-not-exist")
