#!/usr/bin/env python3
"""Run the private checkout's checks without exposing source diagnostics in public CI."""
from pathlib import Path
import subprocess
import sys

source = Path(sys.argv[1]).resolve()
for label, args in (("Tests", ["bash", "scripts/test.sh"]),
                    ("Release build", ["bash", "scripts/build.sh", "release"])):
    # ASVS 13.4.1: publish only the app bundle, never source or private build logs.
    with (source / f"{label.lower().replace(' ', '-')}.log").open("w") as log:
        result = subprocess.run(args, cwd=source, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit(f"{label} failed. Reproduce this source revision locally for diagnostics.")
    print(f"{label} passed", flush=True)
