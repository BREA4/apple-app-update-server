#!/usr/bin/env python3
"""Run the private checkout's checks without exposing source diagnostics in public CI."""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import urllib.request


def diagnose(path, finished):
    """Temporary stage markers; private paths and test names never reach stdout."""
    started = time.monotonic()
    last_sample = started
    engine_downloaded = False
    with path.open() as log:
        while True:
            for line in log:
                line = line.strip()
                event = None
                if line.startswith(("Fetching ", "Fetched ", "Building for ", "Build complete!")):
                    event = line.split(" ", 1)[0] + " Swift dependency/compiler stage"
                elif match := re.match(r"Test Case '([^']+)' (started|passed|failed)", line):
                    event = "Test " + hashlib.sha256(match[1].encode()).hexdigest()[:16] + " " + match[2]
                elif "Mach-O 64-bit executable arm64" in line:
                    event = "Engine download verified"
                    engine_downloaded = True
                elif line.startswith("breachctl:"):
                    # This synthetic smoke fixture has no user profile or credentials.
                    # Redact runtime paths and token-shaped values before publishing.
                    event = re.sub(r"/\S+", "<path>", line)
                    event = re.sub(r"[A-Za-z0-9+/=_-]{24,}", "<REDACTED>", event)[:400]
                elif "No such file or directory" in line:
                    event = "Smoke fixture executable or file is missing"
                elif line.startswith('{"checks":'):
                    event = "Engine smoke test completed" if json.loads(line).get("ok") else "Engine smoke test failed"
                if event:
                    print("[DEBUG-ci-9d2f] " + event, flush=True)
            if finished.is_set():
                return
            if engine_downloaded:
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open("http://127.0.0.1:27891/", timeout=1) as response:
                        origin = {"status": response.status, "sentinel": b"breach-smoke-ok" in response.read(100)}
                except Exception as error:
                    origin = {"error": type(error).__name__}
                print("[DEBUG-ci-9d2f] Smoke origin " + json.dumps(origin), flush=True)
            finished.wait(2)
            if time.monotonic() - last_sample >= 60:
                rows = subprocess.run(["ps", "-axo", "comm=,rss=,pcpu="], capture_output=True, text=True).stdout
                samples = collections.Counter()
                for row in rows.splitlines():
                    parts = row.rsplit(None, 2)
                    if len(parts) != 3:
                        continue
                    name = Path(parts[0]).name
                    group = "tests" if name.endswith(".xctest") else "compiler" if name == "swift-frontend" else "network" if name in ("curl", "git-remote-https") else None
                    if group:
                        samples[group + "Count"] += 1
                        samples[group + "RSSMiB"] += int(parts[1]) / 1024
                        samples[group + "CPU"] += float(parts[2].replace(",", "."))
                print("[DEBUG-ci-9d2f] " + json.dumps({"elapsed": round(time.monotonic() - started), **samples}), flush=True)
                last_sample = time.monotonic()

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
    finished = threading.Event()
    with path.open("w") as log:
        process = subprocess.Popen(args, cwd=source, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, env={**os.environ, "NSUnbufferedIO": "YES"})
        observer = None
        if os.environ.get("BREACH_CI_DIAGNOSTICS") == "1":
            observer = threading.Thread(target=diagnose, args=(path, finished), daemon=True)
            observer.start()
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
            finished.set()
            if observer:
                observer.join(timeout=3)
    if timed_out or process.returncode:
        print(f"{label} subprocess exit status: {process.returncode}", flush=True)
        raise SystemExit(f"{label} failed. Reproduce this source revision locally for diagnostics.")
    print(f"{label} passed", flush=True)
