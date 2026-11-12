"""Validate an output deck: structure, predicted text overflow, rendered previews.

Overflow is predicted by measuring text with real font metrics (no rendering),
then LibreOffice renders previews for human eyes. Font measurement is an
approximation (no kerning/hyphenation, simplified bullet indents) - good enough
to catch "this title no longer fits", not pixel truth.
"""
from __future__ import annotations

import os
import subprocess
import zipfile
from collections import Counter

from lxml import etree
from PIL import ImageFont
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn

import pwd
import shutil

_REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir          # the sandbox replaces $HOME, so ask the OS
FONT_DIRS = ["/usr/share/fonts/truetype", "/usr/share/fonts", os.path.join(_REAL_HOME, "Library/Fonts"),
             "/Library/Fonts", "/opt/homebrew/share/fonts", "/usr/local/share/fonts"]
FALLBACK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"]
# Metric-compatible substitutes: same advance widths as the MS originals.
FONT_SUBST = {"calibri": "Carlito", "arial": "LiberationSans", "times new roman": "LiberationSerif",
              "cambria": "Caladea", "helvetica": "LiberationSans"}
EMU_PER_PT = 12700


def _font_file(name: str, bold: bool):
    """The TrueType file for a font name and weight, with metric-compatible substitutes
    (Calibri -> Carlito, Arial -> Liberation Sans) searched in the font directories.

    Called by: predict_overflow, to measure text with the real letter widths.
    """
    fam = FONT_SUBST.get(name.lower(), name).replace(" ", "")
    style = "Bold" if bold else "Regular"
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower() in (f"{fam}-{style}.ttf".lower(), f"{fam}.ttf".lower()):
                    return os.path.join(root, f)
    for f in FALLBACK_FONTS:
        if os.path.exists(f):
            return f
    raise FileNotFoundError("no TrueType font found; install Carlito and Liberation fonts (see README)")


def _theme_fonts(prs):
    """The theme's heading and body fonts (major, minor), for text that does not name its own."""
    theme = etree.fromstring(prs.slide_master.part.part_related_by(RT.THEME).blob)
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    major = theme.xpath("//a:majorFont/a:latin/@typeface", namespaces=ns)
    minor = theme.xpath("//a:minorFont/a:latin/@typeface", namespaces=ns)
    return (major[0] if major else "Calibri"), (minor[0] if minor else "Calibri")


def _style_sz(txBody_or_style, lvl):
    """The default font size for a paragraph level in a list style, or None."""
    if txBody_or_style is None:
        return None
    d = txBody_or_style.find(f".//{qn(f'a:lvl{lvl}pPr')}/{qn('a:defRPr')}")
    if d is not None and d.get("sz"):
        return int(d.get("sz")) / 100
    return None


def _effective_size(run, para, shape, master):
    """The font size a run really has: its own, else its paragraph's level default, else the
    shape's, the layout's and the master's - the inheritance PowerPoint applies.

    Called by: predict_overflow.
    """
    rPr = run.find(qn("a:rPr"))
    if rPr is not None and rPr.get("sz"):
        return int(rPr.get("sz")) / 100
    pPr = para.find(qn("a:pPr"))
    lvl = int(pPr.get("lvl", 0)) + 1 if pPr is not None else 1
    chain = [shape]
    base = getattr(shape, "_base_placeholder", None) if shape.is_placeholder else None
    while base is not None:
        chain.append(base)
        base = getattr(base, "_base_placeholder", None) if base.is_placeholder else None
    for s in chain:
        lst = s._element.find(f"{qn('p:txBody')}/{qn('a:lstStyle')}")
        sz = _style_sz(lst, lvl)
        if sz:
            return sz
    styles = master._element.find(qn("p:txStyles"))
    is_title = shape.is_placeholder and shape.placeholder_format.type in (1, 3)  # TITLE, CENTER_TITLE
    key = "p:titleStyle" if is_title else ("p:bodyStyle" if shape.is_placeholder else "p:otherStyle")
    return _style_sz(styles.find(qn(key)), lvl) or 18.0


def _wrap_lines(text, font, width):
    """How many lines a text takes in a width at a font, wrapping on spaces like a renderer.

    Called by: predict_overflow.
    """
    lines = 0
    for hard in text.split("\n"):
        words, cur = hard.split(" "), ""
        lines += 1
        for w in words:
            trial = f"{cur} {w}" if cur else w
            if font.getlength(trial) <= width or not cur:
                cur = trial
            else:
                lines, cur = lines + 1, w
    return lines


def text_shapes(shapes):
    """Every shape holding text, groups opened up.

    A shape inside a group is as much on the slide as one outside it. Before group support the
    kit never put text inside a group, so looking only at the top level missed nothing; with it,
    cards, figures and labels routinely live inside groups, and a check that cannot see them
    passes a slide whose text runs out of its box.
    """
    for sh in shapes:
        if getattr(sh, "shape_type", None) == 6 or sh._element.tag == qn("p:grpSp"):
            yield from text_shapes(sh.shapes)
        elif sh.has_text_frame and sh.text_frame.text.strip():
            yield sh


def _slide_box(shape):
    """(left, top, width, height) in slide EMU. A group scales its children's boxes (but not
    their font sizes), so a child's own numbers are not the size it is drawn at."""
    from .refs import to_slide
    return to_slide(shape._element, shape.left or 0, shape.top or 0, shape.width or 0, shape.height or 0)


def predict_overflow(prs, slide, shape, major, minor):
    """Measure whether a shape's text fits its box: lines at the real font and size, times line
    height, against the box drawn on the slide (groups' scaling applied). -> needed and available
    height, ratio, overflow, and a font size that would fit.

    Called by: validate, for every text shape.
    """
    tf = shape.text_frame
    bodyPr = tf._txBody.find(qn("a:bodyPr"))
    ins = {k: int(bodyPr.get(k, d)) / EMU_PER_PT for k, d in
           (("lIns", 91440), ("rIns", 91440), ("tIns", 45720), ("bIns", 45720))}
    _, _, box_w, box_h = _slide_box(shape)
    width = box_w / EMU_PER_PT - ins["lIns"] - ins["rIns"]
    height = box_h / EMU_PER_PT - ins["tIns"] - ins["bIns"]
    is_body = shape.is_placeholder and shape.placeholder_format.type in (2, 7)  # BODY, OBJECT
    if is_body:
        width -= 0.3 * 72                                   # bullet indent, approx.
    scale = 1.0
    na = bodyPr.find(qn("a:normAutofit"))
    if na is not None and na.get("fontScale"):
        scale = int(na.get("fontScale")) / 100000
    # shrink-on-overflow inherited from layout/master: renderers will silently shrink the text
    autofit = na is not None
    base = getattr(shape, "_base_placeholder", None) if shape.is_placeholder else None
    while base is not None and not autofit:
        autofit = base._element.find(f".//{qn('a:bodyPr')}/{qn('a:normAutofit')}") is not None
        base = getattr(base, "_base_placeholder", None) if base.is_placeholder else None
    total, max_size = 0.0, 0.0
    for p in tf._txBody.findall(qn("a:p")):
        runs = p.findall(qn("a:r"))
        text = "".join((r.find(qn("a:t")).text or "") for r in runs)
        ref_run = runs[0] if runs else p
        size = _effective_size(ref_run, p, shape, slide.slide_layout.slide_master) * scale
        max_size = max(max_size, size)
        is_title = shape.is_placeholder and shape.placeholder_format.type in (1, 3)
        face = major if is_title else minor
        latin = ref_run.find(f"{qn('a:rPr')}/{qn('a:latin')}") if runs else None
        if latin is not None and not latin.get("typeface", "+").startswith("+"):
            face = latin.get("typeface")
        bold = bool(runs) and any((r.find(qn("a:rPr")) is not None and r.find(qn("a:rPr")).get("b") == "1") for r in runs)
        font = ImageFont.truetype(_font_file(face, bold), max(int(round(size)), 1))
        n = _wrap_lines(text, font, width) if text else 1
        total += n * size * (0.9 if is_title else 1.2) + (0.2 * size if is_body else 0)
    over = total - height
    if height <= 0:                      # a box squeezed to nothing can't hold any text
        return {"needed_pt": round(total, 1), "available_pt": round(height, 1), "ratio": 99,
                "overflow": bool(total), "max_font_pt": round(max_size, 1), "masked_by_autofit": autofit,
                "suggest_size_pt": None}
    return {"needed_pt": round(total, 1), "available_pt": round(height, 1),
            "ratio": round(total / height, 3) if height else 99,
            "overflow": over > 0.02 * height, "max_font_pt": round(max_size, 1),
            "masked_by_autofit": autofit,
            "suggest_size_pt": round(max_size * min(1.0, (height / total) ** 0.5) * 0.95, 1) if total else None}


def find_overlaps(slide, min_share=0.15):
    """Pairs of text-holding shapes whose boxes overlap by more than min_share of the smaller box.
    Text inside its own background box is not a problem (the box holds no text)."""
    boxes = []
    for sh in text_shapes(slide.shapes):
        x, y, w, h = _slide_box(sh)
        if w and h:
            boxes.append((sh.shape_id, x, y, x + w, y + h))
    out = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            w = min(a[3], b[3]) - max(a[1], b[1])
            h = min(a[4], b[4]) - max(a[2], b[2])
            if w <= 0 or h <= 0:
                continue
            smaller = min((a[3] - a[1]) * (a[4] - a[2]), (b[3] - b[1]) * (b[4] - b[2]))
            share = w * h / smaller
            if share > min_share:
                out.append({"kind": "overlap", "shapes": [a[0], b[0]], "share": round(share, 2)})
    return out


def structural_checks(path):
    """Problems that would make PowerPoint refuse or repair the file: parts with no content type,
    duplicate shape ids on a slide.

    Called by: validate.
    """
    problems = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        ct = etree.fromstring(z.read("[Content_Types].xml"))
        ns = "{http://schemas.openxmlformats.org/package/2006/content-types}"
        defaults = {d.get("Extension").lower() for d in ct.iter(ns + "Default")}
        overrides = {o.get("PartName") for o in ct.iter(ns + "Override")}
        for n in names:
            if n.endswith("/") or n == "[Content_Types].xml":
                continue
            if "/" + n not in overrides and n.rsplit(".", 1)[-1].lower() not in defaults:
                problems.append(f"no content type for {n}")
    prs = Presentation(path)
    for s in prs.slides:
        ids = Counter(e.get("id") for e in s.shapes._spTree.iter(qn("p:cNvPr")))
        dup = [i for i, c in ids.items() if c > 1]
        if dup:
            problems.append(f"slide {s.slide_id}: duplicate shape ids {dup}")
    return problems


def render(path, outdir, pages=None, dpi=60):
    """Render a deck to PDF with a local LibreOffice, then chosen pages to PNG with pdftoppm.

    Called by: the agent's render_backend='local' (sandbox.render._render_local); the sandbox image
    has its own copy (runner/render.py).
    """
    os.makedirs(outdir, exist_ok=True)
    soffice = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    subprocess.run([soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", outdir, path],
                   check=True, capture_output=True, timeout=180)
    pdf = os.path.join(outdir, os.path.splitext(os.path.basename(path))[0] + ".pdf")
    pngs = []
    for pg in pages or []:
        stem = os.path.join(outdir, f"slide-{pg + 1:02d}")
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(pg + 1), "-l", str(pg + 1),
                        "-singlefile", pdf, stem], check=True, capture_output=True, timeout=60)
        pngs.append(stem + ".png")
    return pdf, pngs


def validate(path, slide_indices, outdir, do_render=True):
    """Run the checks on a deck (or some slides): overflow and overlap per slide, structural
    problems for the file, and optionally render previews. -> {findings, structural[, previews]}

    Called by: the agent's engine.deck.check (with do_render=False) after every build and edit.
    """
    prs = Presentation(path)
    major, minor = _theme_fonts(prs)
    findings = []
    for i, s in enumerate(prs.slides):
        if slide_indices is not None and i not in slide_indices:
            continue
        for shp in text_shapes(s.shapes):
            if True:
                r = predict_overflow(prs, s, shp, major, minor)
                if r["overflow"]:
                    findings.append({"kind": "overflow", "ref": f"slide:{s.slide_id}/shape:{shp.shape_id}",
                                     "slide_index": i, **r})
        for o in find_overlaps(s):
            findings.append({**o, "slide_index": i, "ref": f"slide:{s.slide_id}/shape:{o['shapes'][0]}"})
    report = {"structural": structural_checks(path), "findings": findings}
    if do_render:
        pages = list(range(len(prs.slides))) if slide_indices is None else sorted(slide_indices)
        pdf, pngs = render(path, outdir, pages)
        report.update(pdf=pdf, previews=pngs)
    return report
