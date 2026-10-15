from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from faultline.cli import main


class CliTests(unittest.TestCase):
    def test_clean_scan_is_successful(self):
        with TemporaryDirectory() as directory:
            output = StringIO()
            with redirect_stdout(output):
                result = main(["scan", directory])

        self.assertEqual(result, 0)
        self.assertIn("0 finding(s)", output.getvalue())

    def test_findings_return_nonzero_and_support_json(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "example.py"
            path.write_text("# BUG: reproduce\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = main(["scan", "--json", str(path)])

        self.assertEqual(result, 1)
        self.assertIn('"marker": "BUG"', output.getvalue())
