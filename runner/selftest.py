"""Prove the sandbox is what we think it is. Runs inside the container.

    python3 /opt/runner/selftest.py out

Checks, and writes the answers to `out/result.json`:
  network      can anything reach the internet from in here?  (must be blocked)
  rootfs       can the image's filesystem be written to?      (must be read-only)
  write_work   can /work be written to?                       (must be yes)
  tools        soffice, pdftoppm, the metric-compatible fonts, python-pptx
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys


def main(argv: list[str]) -> int:
    """Check, from inside the container, that it has no network, a read-only root filesystem, and
    the tools and fonts rendering needs. Writes report.json.

    Called by: `deckops selftest` / `make selftest`.
    """
    outdir = argv[0] if argv else "out"
    os.makedirs(outdir, exist_ok=True)
    res: dict = {}

    try:
        socket.setdefaulttimeout(4)
        socket.create_connection(("1.1.1.1", 443), timeout=4)
        res["network"] = "REACHABLE"
    except OSError as e:
        res["network"] = f"blocked ({e.__class__.__name__})"
    try:
        socket.getaddrinfo("example.com", 443)
        res["dns"] = "RESOLVES"
    except OSError as e:
        res["dns"] = f"blocked ({e.__class__.__name__})"

    try:
        with open("/usr/local/_probe", "w") as f:
            f.write("x")
        os.remove("/usr/local/_probe")
        res["rootfs"] = "WRITABLE"
    except OSError as e:
        res["rootfs"] = f"read-only ({e.__class__.__name__})"

    try:
        with open(os.path.join(outdir, "_probe"), "w") as f:
            f.write("x")
        os.remove(os.path.join(outdir, "_probe"))
        res["write_work"] = "yes"
    except OSError as e:
        res["write_work"] = f"NO ({e})"

    res["uid"] = os.getuid()
    res["soffice"] = shutil.which("soffice")
    res["pdftoppm"] = shutil.which("pdftoppm")
    try:
        import pptx
        res["python_pptx"] = pptx.__version__
    except Exception as e:
        res["python_pptx"] = f"missing ({e})"
    fonts = subprocess.run(["bash", "-lc", "ls /usr/share/fonts/truetype/*/ | sort | head -40"],
                           capture_output=True, text=True)
    res["fonts"] = [ln for ln in fonts.stdout.split() if ln.endswith(".ttf")][:20]
    res["ok"] = res["network"].startswith("blocked") and res["rootfs"].startswith("read-only") \
        and res["write_work"] == "yes" and bool(res["soffice"]) and bool(res["pdftoppm"])
    with open(os.path.join(outdir, "result.json"), "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
