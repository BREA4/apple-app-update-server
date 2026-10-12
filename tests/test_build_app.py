from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest

WRAPPER = Path(__file__).resolve().parents[1] / "scripts/build-app.py"


class PrivateBuildTests(unittest.TestCase):
    def test_failure_identifies_tests_without_exposing_private_diagnostics(self):
        identifier = "-[PrivateTests.SecretClass testPrivateBehavior]"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "scripts").mkdir()
            (source / "scripts/test.sh").write_text("cat <<'LOG'\n"
                f"Test Case '{identifier}' started.\n"
                f"/private/source.swift:12: error: {identifier} : XCTAssertEqual failed - PRIVATE_CREDENTIAL\n"
                f"Test Case '{identifier}' failed (0.1 seconds).\n"
                "/private/source.swift:25: error: PRIVATE_SOURCE_DETAIL\n"
                "LOG\nexit 1\n")
            result = subprocess.run([sys.executable, str(WRAPPER), str(source), "--phase", "tests"],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            summary = json.loads(next(line.split("failure summary: ", 1)[1] for line in result.stdout.splitlines() if "failure summary:" in line))
            self.assertEqual(summary["failedTestIDs"], [hashlib.sha256(identifier.encode()).hexdigest()[:16]])
            self.assertEqual(summary["assertions"], ["XCTAssertEqual"])
            self.assertEqual(summary["otherErrorCount"], 1)
            for private in ["PrivateTests", "SecretClass", "testPrivateBehavior", "/private/source.swift", "PRIVATE_CREDENTIAL", "PRIVATE_SOURCE_DETAIL"]:
                self.assertNotIn(private, result.stdout + result.stderr)

    def test_timeout_fails_even_if_the_child_handles_termination_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "scripts").mkdir()
            (source / "scripts/test.sh").write_text("trap 'exit 0' TERM\nprintf 'PRIVATE_DIAGNOSTIC\\n'\nsleep 30\n")
            started = time.monotonic()
            result = subprocess.run([sys.executable, str(WRAPPER), str(source), "--phase", "tests", "--timeout", "1"],
                                    capture_output=True, text=True, timeout=10)
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
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((source / "invocation").read_text(), "release")
            self.assertFalse((source / "tests.log").exists())
            self.assertNotIn("PRIVATE_BUILD_DETAIL", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
