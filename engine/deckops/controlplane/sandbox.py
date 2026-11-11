"""Run a data-plane command in a sandbox.

POC sandbox, a scrubbed environment and a hard timeout, plus:
  Linux: user + network namespace (`unshare -rn`, so no network at all) and rlimits on
         memory/CPU/file size/open files (`prlimit`).
  macOS: `sandbox-exec` with a profile that denies all IP traffic, and rlimits on CPU/file
         size/open files (macOS does not enforce a memory limit).
The data plane's `selftest` command proves the network is blocked on either system.

Production: replace `run()` with "claim a Kata microVM, hand it file refs + JSON,
wait for JSON back". The contract (argv in, files + JSON report out) is the same,
which is the point - the control plane never learns which sandbox it got.

Known gap of the POC sandbox: no mount namespace, so the process can still READ
the host filesystem. Kata closes that; so would bubblewrap.
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "dataplane_main.py")
MACOS = sys.platform == "darwin"
# macOS: allow everything except networking; local unix sockets (used by LibreOffice) stay allowed
MAC_PROFILE = "(version 1)(allow default)(deny network*)(allow network* (remote unix-socket))"
# where the tools live: Linux system dirs, Homebrew (Apple silicon, Intel), LibreOffice.app
TOOL_PATH = ":".join(["/usr/local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin",
                      "/Applications/LibreOffice.app/Contents/MacOS"])


class SandboxError(Exception):
    pass


def run(args: list[str], workspace: str, report_path: str, timeout=240, mem_mb=4096, cpu_s=180):
    workspace = os.path.abspath(workspace)
    home = os.path.join(workspace, ".home")
    os.makedirs(home, exist_ok=True)
    py = [sys.executable, "-E", "-s", ENTRY, *args]
    limits = None
    if MACOS:
        sbx = shutil.which("sandbox-exec")
        if not sbx:
            raise SandboxError("sandbox-exec not found (it ships with macOS)")
        cmd = [sbx, "-p", MAC_PROFILE, *py]

        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
    else:
        cmd = ["unshare", "-rn", "--",
               "prlimit", f"--as={mem_mb * 1024 * 1024}", f"--cpu={cpu_s}",
               f"--fsize={512 * 1024 * 1024}", "--nofile=512", "--", *py]
    env = {"HOME": home, "PATH": TOOL_PATH, "LANG": "en_US.UTF-8" if MACOS else "C.UTF-8", "TMPDIR": home}
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True, timeout=timeout,
                              preexec_fn=limits)
    except subprocess.TimeoutExpired:
        raise SandboxError(f"{args[0]} timed out after {timeout}s")
    elapsed = round(time.time() - t0, 2)
    try:
        report = json.load(open(report_path))
    except Exception:
        raise SandboxError(f"{args[0]} produced no report (rc={proc.returncode}): {proc.stderr[-800:]}")
    if isinstance(report, dict):
        report.setdefault("_sandbox", {"rc": proc.returncode, "seconds": elapsed})
    return proc.returncode, report
