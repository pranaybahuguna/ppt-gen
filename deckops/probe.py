"""Probe: .pptx -> DeckIndex (the read model every other stage works from).

Records what it can edit *and* what it can't (SmartArt, charts, OLE), so the
planner never plans an impossible edit.
"""
from __future__ import annotations

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.oxml.ns import qn

from .refs import para_runs, para_tagged, para_text, sha, text_body_paragraphs, to_slide

DGM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"
OLE_URI = "http://schemas.openxmlformats.org/presentationml/2006/ole"

ROLE_BY_PH = {
    PP_PLACEHOLDER.TITLE: "title",
    PP_PLACEHOLDER.CENTER_TITLE: "title",
    PP_PLACEHOLDER.SUBTITLE: "subtitle",
    PP_PLACEHOLDER.BODY: "body",
    PP_PLACEHOLDER.OBJECT: "body",
    PP_PLACEHOLDER.PICTURE: "picture",
    PP_PLACEHOLDER.CHART: "visual",
    PP_PLACEHOLDER.TABLE: "visual",
}


def placeholder_role(ph) -> str:
    return ROLE_BY_PH.get(ph.placeholder_format.type, "chrome")  # date/footer/slide no.


def _paragraphs(base_ref: str, txBody):
    out = []
    for i, p in enumerate(text_body_paragraphs(txBody)):
        text = para_text(p)
        ref = f"{base_ref}/para:{i}"
        out.append({
            "ref": ref,
            "text": text,
            "sha": sha(text),
            "tagged": para_tagged(p),
            "runs": len(para_runs(p)),
        })
    return out


def _geom(shape):
    """Where the shape is, in slide coordinates.

    A shape inside a group states its position in that group's child coordinate space, which
    is meaningless to anything comparing it with the rest of the slide. `to_slide` converts it;
    for a shape sitting directly on the slide it is the identity. See deckops/refs.py.
    """
    try:
        x, y, w, h = to_slide(shape._element, int(shape.left or 0), int(shape.top or 0),
                              int(shape.width or 0), int(shape.height or 0))
        return {"x": x, "y": y, "w": w, "h": h}
    except Exception:
        return None


def _shape_entries(slide_ref: str, shapes, unsupported: list):
    for shp in shapes:
        ref = f"{slide_ref}/shape:{shp.shape_id}"
        entry = {"ref": ref, "name": shp.name, "geom": _geom(shp), "editable": True,
                 "opaque_reason": None}
        if shp.is_placeholder:
            entry["kind"] = "placeholder"
            entry["role"] = placeholder_role(shp)
            entry["ph_idx"] = shp.placeholder_format.idx
        if shp.shape_type == MSO_SHAPE_TYPE.GROUP:
            entry["kind"] = "group"
            yield entry
            yield from _shape_entries(slide_ref, shp.shapes, unsupported)
            continue
        el = shp._element
        gd = el.find(".//" + qn("a:graphicData"))
        uri = gd.get("uri") if gd is not None else None
        if getattr(shp, "has_table", False) and shp.has_table:
            entry["kind"] = "table"
            entry["cells"] = []
            for r, row in enumerate(shp.table.rows):
                for c, cell in enumerate(row.cells):
                    entry["cells"].append({
                        "rc": [r, c],
                        "paras": _paragraphs(f"{ref}/cell:{r},{c}", cell._tc.txBody),
                    })
        elif uri in (DGM_URI, CHART_URI, OLE_URI):
            kind = {DGM_URI: "smartart", CHART_URI: "chart", OLE_URI: "ole"}[uri]
            entry.update(kind=kind, editable=False,
                         opaque_reason=f"{kind}: can move/delete, cannot edit contents in this version")
            unsupported.append({"ref": ref, "kind": kind, "note": entry["opaque_reason"]})
        elif shp.has_text_frame:
            entry.setdefault("kind", "text")
            entry["paras"] = _paragraphs(ref, shp.text_frame._txBody)
            sizes = [r.font.size.pt for p in shp.text_frame.paragraphs for r in p.runs if r.font.size]
            entry["font_pt"] = max(sizes) if sizes else None
        elif shp.shape_type == MSO_SHAPE_TYPE.PICTURE:
            entry.setdefault("kind", "picture")
            try:
                import hashlib
                entry["image_sha"] = hashlib.sha256(shp.image.blob).hexdigest()[:16]
            except Exception:
                entry["image_sha"] = None
        else:
            entry.setdefault("kind", "other")
        yield entry


def layout_catalog(prs):
    cat = []
    for mi, master in enumerate(prs.slide_masters):
        for li, layout in enumerate(master.slide_layouts):
            phs = [{
                "idx": ph.placeholder_format.idx,
                "role": placeholder_role(ph),
                "x": ph.left, "y": ph.top, "w": ph.width, "h": ph.height,
            } for ph in layout.placeholders]
            cat.append({"id": f"M{mi}L{li}", "name": layout.name, "placeholders": phs})
    return cat


def external_rels(prs):
    """Relationships pointing outside the package: a renderer may try to fetch these."""
    out = []
    for part in prs.part.package.iter_parts():
        for rel in part.rels.values():
            if rel.is_external:
                out.append({"part": str(part.partname), "target": rel.target_ref})
    return out


def probe(path: str) -> dict:
    prs = Presentation(path)
    unsupported: list = []
    slides = []
    for idx, s in enumerate(prs.slides):
        sref = f"slide:{s.slide_id}"
        entry = {
            "ref": sref, "index": idx, "layout": s.slide_layout.name,
            "shapes": list(_shape_entries(sref, s.shapes, unsupported)),
            "notes": None,
        }
        if s.has_notes_slide:
            entry["notes"] = _paragraphs(f"{sref}/notes", s.notes_slide.notes_text_frame._txBody)
        slides.append(entry)
    return {
        "slide_size": {"w": prs.slide_width, "h": prs.slide_height},
        "layouts": layout_catalog(prs),
        "slides": slides,
        "unsupported": unsupported,
        "external_rels": external_rels(prs),
    }


def iter_paragraphs(index: dict, slide_indices=None):
    """Every translatable paragraph in the DeckIndex (shapes, table cells, notes)."""
    for s in index["slides"]:
        if slide_indices is not None and s["index"] not in slide_indices:
            continue
        for shp in s["shapes"]:
            if not shp.get("editable", True):
                continue
            yield from shp.get("paras", [])
            for cell in shp.get("cells", []):
                yield from cell["paras"]
        yield from (s["notes"] or [])
