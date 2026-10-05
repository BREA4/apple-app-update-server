#!/usr/bin/env python3
"""Run the private checkout's checks without exposing source diagnostics in public CI."""
import argparse
import os
from pathlib import Path
import signal
import subprocess


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
        raise SystemExit(f"{label} failed. Reproduce this source revision locally for diagnostics.")
    print(f"{label} passed", flush=True)
