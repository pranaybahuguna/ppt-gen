"""Render a deck to PNG previews. Runs inside the sandbox container.

    python3 /opt/runner/render.py in/deck.pptx out 0,1,2 60

Writes `out/result.json` and `out/slide-NN.png`. This is the engine's own rendering recipe
(LibreOffice to PDF, poppler to PNG per page); it lives here because `soffice` and `pdftoppm`
are the only native binaries the project needs, and keeping them in the image means the agent
itself needs nothing installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback


def render(pptx: str, outdir: str, pages: list[int], dpi: int) -> dict:
    os.makedirs(outdir, exist_ok=True)
    soffice = shutil.which("soffice") or "/usr/bin/soffice"
    # LibreOffice insists on creating a user profile before it will start, and it cannot work
    # out where to put one when the container runs as a uid with no /etc/passwd entry - which
    # is exactly what happens here, because containers run as the *host* user's uid so that
    # files in /work come back owned by us. So the profile is placed explicitly, on the
    # container's tmpfs: writable, and gone when the container is.
    subprocess.run([soffice, "-env:UserInstallation=file:///tmp/libreoffice-profile",
                    "--headless", "--norestore", "--nolockcheck", "--nodefault",
                    "--convert-to", "pdf", "--outdir", outdir, pptx],
                   check=True, capture_output=True, timeout=240)
    pdf = os.path.join(outdir, os.path.splitext(os.path.basename(pptx))[0] + ".pdf")
    if not os.path.exists(pdf):
        raise RuntimeError("LibreOffice produced no PDF")
    pngs = []
    for pg in pages:
        stem = os.path.join(outdir, f"slide-{pg + 1:02d}")
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(pg + 1), "-l", str(pg + 1),
                        "-singlefile", pdf, stem], check=True, capture_output=True, timeout=120)
        pngs.append(os.path.basename(stem + ".png"))
    return {"ok": True, "pdf": os.path.basename(pdf), "previews": pngs, "pages": pages}


def main(argv: list[str]) -> int:
    pptx, outdir, pages_csv, dpi = argv[0], argv[1], argv[2], int(argv[3])
    pages = [int(x) for x in pages_csv.split(",") if x != ""]
    os.makedirs(outdir, exist_ok=True)
    try:
        result = render(pptx, outdir, pages, dpi)
        rc = 0
    except subprocess.CalledProcessError as e:
        result = {"ok": False, "error": "render failed",
                  "stderr": (e.stderr or b"").decode(errors="replace")[-2000:]}
        rc = 1
    except Exception:
        result = {"ok": False, "error": "render crashed", "traceback": traceback.format_exc(limit=5)}
        rc = 1
    with open(os.path.join(outdir, "result.json"), "w") as f:
        json.dump(result, f, indent=1)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
