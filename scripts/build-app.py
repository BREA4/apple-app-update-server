#!/usr/bin/env python3
"""Run the private checkout's checks without exposing source diagnostics in public CI."""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path)
parser.add_argument("--phase", choices=("tests", "build", "all"), default="all")
options = parser.parse_args()
source = options.source.resolve()
for phase, label, args in (("tests", "Tests", ["bash", "scripts/test.sh"]),
                          ("build", "Release build", ["bash", "scripts/build.sh", "release"])):
    if options.phase not in (phase, "all"):
        continue
    print(f"{label} started", flush=True)
    # ASVS 13.4.1: publish only the app bundle, never source or private build logs.
    with (source / f"{label.lower().replace(' ', '-')}.log").open("w") as log:
        result = subprocess.run(args, cwd=source, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit(f"{label} failed. Reproduce this source revision locally for diagnostics.")
    print(f"{label} passed", flush=True)
