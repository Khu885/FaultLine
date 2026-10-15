from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from faultline.localizer import localize_source_line, localize_traceback


class LocalizerTests(unittest.TestCase):
    def test_nested_functions_and_class_method_choose_smallest_enclosing_node(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "app.py"
            path.write_text(
                "class Worker:\n"
                "    def run(self):\n"
                "        def inner():\n"
                "            return 1\n"
                "        return inner()\n",
                encoding="utf-8",
            )

            location = localize_source_line(path, 4, root)

        self.assertEqual(location.status, "resolved")
        self.assertEqual(location.function, "Worker.run.inner")
        self.assertEqual(location.category, "application")

    def test_async_function_and_relative_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            path = root / "pkg" / "service.py"
            path.write_text("async def fetch():\n    return 42\n", encoding="utf-8")

            location = localize_source_line("pkg/service.py", 2, root)

        self.assertEqual(location.function, "fetch")
        self.assertEqual(location.path, str(path.resolve()))

    def test_test_assertion_is_classified_but_application_frame_is_preferred(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            test_path = root / "tests" / "test_app.py"
            app_path = root / "app.py"
            test_path.write_text("def test_value():\n    assert call() == 3\n", encoding="utf-8")
            app_path.write_text("def call():\n    return 2\n", encoding="utf-8")

            assertion = localize_source_line(test_path, 2, root)
            selected = localize_traceback(
                [(str(test_path), 2), (str(app_path), 2)], root
            )

        self.assertEqual(assertion.category, "test_assertion")
        self.assertTrue(assertion.is_assertion)
        self.assertEqual(selected.function, "call")

    def test_outside_project_and_malformed_source_are_unresolved(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root = Path(directory)
            outside_path = Path(outside) / "other.py"
            outside_path.write_text("def other():\n    return 1\n", encoding="utf-8")
            malformed = root / "broken.py"
            malformed.write_text("def broken(:\n", encoding="utf-8")

            outside_location = localize_source_line(outside_path, 2, root)
            malformed_location = localize_source_line(malformed, 1, root)

        self.assertEqual(outside_location.status, "unresolved")
        self.assertIn("outside", outside_location.reason)
        self.assertEqual(malformed_location.status, "unresolved")
        self.assertIn("valid Python", malformed_location.reason)


if __name__ == "__main__":
    unittest.main()
