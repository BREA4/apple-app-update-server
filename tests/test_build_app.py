from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

WRAPPER = Path(__file__).resolve().parents[1] / "scripts/build-app.py"


class PrivateBuildTests(unittest.TestCase):
    def test_timeout_fails_even_if_the_child_handles_termination_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "scripts").mkdir()
            (source / "scripts/test.sh").write_text("trap 'exit 0' TERM\nprintf 'PRIVATE_DIAGNOSTIC\\n'\nsleep 30\n")
            started = time.monotonic()
            result = subprocess.run([sys.executable, str(WRAPPER), str(source), "--phase", "tests", "--timeout", "1"],
                                    capture_output=True, text=True, timeout=10,
                                    env={**os.environ, "BREACH_CI_DIAGNOSTICS": "0"})
            self.assertNotEqual(result.returncode, 0)
            self.assertLess(time.monotonic() - started, 8)
            self.assertIn("exceeded 1 seconds", result.stdout)
            self.assertNotIn("PRIVATE_DIAGNOSTIC", result.stdout + result.stderr)
            self.assertIn("PRIVATE_DIAGNOSTIC", (source / "tests.log").read_text())

    def test_release_phase_passes_release_argument_and_keeps_diagnostics_private(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "scripts").mkdir()
            (source / "scripts/test.sh").write_text("exit 17\n")
            (source / "scripts/build.sh").write_text("printf '%s' \"$1\" > invocation\nprintf 'PRIVATE_BUILD_DETAIL\\n'\n")
            result = subprocess.run([sys.executable, str(WRAPPER), str(source), "--phase", "build"],
                                    capture_output=True, text=True, timeout=10,
                                    env={**os.environ, "BREACH_CI_DIAGNOSTICS": "0"})
            self.assertEqual(result.returncode, 0)
            self.assertEqual((source / "invocation").read_text(), "release")
            self.assertFalse((source / "tests.log").exists())
            self.assertNotIn("PRIVATE_BUILD_DETAIL", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
