"""The CLI must remain inert until an explicit, bounded command is selected."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.l16_lab import __main__ as cli


class CliTests(unittest.TestCase):
    def test_default_and_asterisk_preview_never_contact_docker(self):
        for args in ([], ["plan"], ["asterisk"], ["asterisk", "--image", "example:test"]):
            with self.subTest(args=args), mock.patch("subprocess.run") as run:
                output = io.StringIO()
                with redirect_stdout(output):
                    result = cli.main(args)
                self.assertEqual(result, 0)
                self.assertFalse(json.loads(output.getvalue())["executed"])
                run.assert_not_called()

    def test_execution_requires_explicit_image_engine_and_output(self):
        with mock.patch("subprocess.run") as run, mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                cli.main(["asterisk", "--execute"])
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    def test_external_output_is_refused_before_running_fixture(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cli, "offline") as offline:
            output = io.StringIO()
            target = Path(folder) / "must-not-exist"
            with redirect_stdout(output):
                result = cli.main(["offline", "--output", str(target)])
            self.assertEqual(result, 2)
            self.assertFalse(target.exists())
            offline.assert_not_called()

    def test_offline_fault_matrix_has_real_expected_failures(self):
        report = cli.offline()
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(report["network_used"])
        for fault, result in report["faults"].items():
            self.assertEqual(result["ok"], fault == "clean")
        self.assertTrue(report["g722_geometry"]["ok"])


if __name__ == "__main__":
    unittest.main()
