#!/usr/bin/env python3
"""Run the private checkout's checks without exposing source diagnostics in public CI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess


def failure_summary(path):
    """Emit counts and opaque test IDs, never source, paths or assertion values."""
    failed = set()
    assertions = set()
    last_test = None
    compiler_errors = 0
    smoke_ready = False
    with path.open(errors="replace") as log:
        for line in log:
            case = re.search(r"Test Case '([^']+)' (started|failed)", line)
            if case:
                identifier = hashlib.sha256(case[1].encode()).hexdigest()[:16]
                last_test = identifier
                if case[2] == "failed":
                    failed.add(identifier)
            if "error:" in line:
                assertion = re.search(r"\b(XCTAssert(?:True|False|Equal|NotEqual|Nil|NotNil|ThrowsError|NoThrow)|XCTUnwrap|XCTFail)\b", line)
                if assertion:
                    assertions.add(assertion[1])
                else:
                    compiler_errors += 1
            smoke_ready |= line.strip() == "Smoke origin ready"
    return {"failedTestIDs": sorted(failed), "lastTestID": last_test,
            "assertions": sorted(assertions), "otherErrorCount": compiler_errors,
            "smokeOriginReady": smoke_ready}


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path)
parser.add_argument("--phase", choices=("tests", "build", "all"), default="all")
parser.add_argument("--timeout", type=int, default=900, help="Maximum seconds for each phase")
options = parser.parse_args()
if options.timeout < 1:
    parser.error("--timeout must be positive")
source = options.source.resolve()
for phase, label, args in (("tests", "Tests", ["bash", "scripts/test.sh"]),
                          ("build", "Release build", ["bash", "scripts/build.sh", "release"])):
    if options.phase not in (phase, "all"):
        continue
    print(f"{label} started", flush=True)
    # ASVS 13.4.1: publish only the app bundle, never source or private build logs.
    path = source / f"{label.lower().replace(' ', '-')}.log"
    with path.open("w") as log:
        process = subprocess.Popen(args, cwd=source, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, env={**os.environ, "NSUnbufferedIO": "YES"})
        timed_out = False
        try:
            process.wait(timeout=options.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            print(f"{label} exceeded {options.timeout} seconds", flush=True)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    if timed_out or process.returncode:
        print(f"{label} subprocess exit status: {process.returncode}", flush=True)
        # ASVS 13.4.1: hashes identify a local test without publishing private names.
        print(f"{label} failure summary: {json.dumps(failure_summary(path), sort_keys=True)}", flush=True)
        raise SystemExit(f"{label} failed. Reproduce this source revision locally for diagnostics.")
    print(f"{label} passed", flush=True)
