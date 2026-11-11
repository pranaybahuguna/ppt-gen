"""Execute an OpList against a source deck.

Two properties the design asserts, enforced here rather than checked afterwards:

1. Bind-then-apply. Every target is resolved and its `expect` fingerprint checked
   against the *source* before anything is mutated. One stale fingerprint fails
   the whole batch, and nothing is written. Positional indices can't drift
   mid-batch because elements are bound up front.

2. Byte-preservation. python-pptx re-serializes every XML part it parsed, so a
   PowerPoint-authored slide nobody touched still comes out with different bytes.
   `save_preserving` compares each part canonically and writes the ORIGINAL bytes
   for any part whose content didn't change. "Untouched slide = identical bytes"
   becomes a structural property of the executor.
"""
from __future__ import annotations

import copy
import os
import zipfile

from lxml import etree
from pptx import Presentation
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement

from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from .ops import (AddSlide, AddSlideFromPattern, DeleteShape, DeleteSlide, DuplicateShape, OpList, SetDocProperties,
                  SetChart, SetFontSize, SetGeometry, SetParagraphText, SetRunText, SetShapeText, SetTable)
from .refs import (Ref, RefError, TagError, find_shape_el, para_runs, para_text, para_tokens,
                   parse_tagged, resolve, sha, to_own, to_slide)

PHASE = {SetParagraphText: 0, SetRunText: 0, SetFontSize: 1, SetGeometry: 1, DeleteShape: 2, AddSlide: 3,
         AddSlideFromPattern: 3, DeleteSlide: 4, SetDocProperties: 5}


class ExecError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(f"op[{i}] {e}" for i, e in errors))
        self.errors = errors


# ---- bind ---------------------------------------------------------------------

def _bind(prs, op):
    if isinstance(op, (SetParagraphText, SetRunText)):
        ref = Ref.parse(op.target)
        kind, el, _ = resolve(prs, ref)
        want = "para" if isinstance(op, SetParagraphText) else "run"
        if kind != want:
            raise RefError(f"{op.op} needs a {want} ref, got {kind}")
        current = para_text(el) if want == "para" else (el.find(qn("a:t")).text or "")
        if sha(current) != op.expect.text_sha:
            raise RefError(f"expect mismatch at {op.target}: deck has changed since it was planned")
        if want == "para":
            _check_tags(el, op.value)
        return {"el": el}
    if isinstance(op, (SetFontSize, SetGeometry, DeleteShape)):
        kind, el, _ = resolve(prs, Ref.parse(op.target))
        if kind != "shape":
            raise RefError(f"{op.op} needs a shape ref")
        if isinstance(op, SetGeometry) and el.find(".//" + qn("a:xfrm")) is None:
            raise RefError(f"{op.target}: shape has no own geometry (inherits from layout)")
        return {"el": el}
    if isinstance(op, DeleteSlide):
        ref = Ref.parse(op.target)
        resolve(prs, ref)
        return {"sldId": _sldId_el(prs, ref.slide)}
    if isinstance(op, SetDocProperties):
        return {}
    if isinstance(op, AddSlideFromPattern):
        ref = Ref.parse(op.source)
        _, _, slide = resolve(prs, ref)
        _check_cloneable(slide)
        known = {int(e.get("id")) for e in slide.shapes._spTree.iter(qn("p:cNvPr"))}
        for k, st in enumerate(op.steps):                     # validate every step before anything runs
            if isinstance(st, DuplicateShape):
                if _sid(st.source) not in known:
                    raise RefError(f"step {k}: no shape {st.source} to duplicate")
                if st.new_id in known:
                    raise RefError(f"step {k}: id {st.new_id} already used")
                known.add(st.new_id)
            elif _sid(st.target) not in known:
                raise RefError(f"step {k}: no shape {st.target} on {op.source}")
        return {"slide": slide}
    if isinstance(op, AddSlide):
        layout = _layout(prs, op.layout)
        after = _sldId_el(prs, Ref.parse(op.after).slide) if op.after else None
        idxs = {ph.placeholder_format.idx for ph in layout.placeholders}
        missing = set(op.fill) - idxs
        if missing:
            raise RefError(f"layout {op.layout} has no placeholder idx {sorted(missing)}")
        return {"layout": layout, "after": after}
    raise RefError(f"unknown op {op}")


def _check_tags(p, value):
    tokens = parse_tagged(value)
    n_runs = len(para_runs(p))
    n_flds = sum(1 for c in p if c.tag == qn("a:fld"))
    for t in tokens:
        if t[0] == "r" and t[1] >= n_runs:
            raise TagError(f"<r{t[1]}> but paragraph has {n_runs} runs")
        if t[0] == "f" and t[1] >= n_flds:
            raise TagError(f"<f{t[1]}/> but paragraph has {n_flds} fields")


def _sldId_el(prs, slide_id):
    for el in prs.slides._sldIdLst:
        if int(el.get("id")) == slide_id:
            return el
    raise RefError(f"no slide id {slide_id}")


def _layout(prs, layout_id):
    try:
        mi, li = layout_id[1:].split("L")
        return prs.slide_masters[int(mi)].slide_layouts[int(li)]
    except Exception:
        raise RefError(f"unknown layout {layout_id}")


# ---- copying a slide ------------------------------------------------------------

# A slide holding one of these cannot be copied, because `clone_slide` shares the part rather
# than duplicating it, and for these that is wrong: two slides pointing at one chart part means
# editing either edits both, and the same goes for a diagram's data, layout and colours.
_NO_CLONE = {RT.CHART, RT.DIAGRAM_DATA, RT.DIAGRAM_LAYOUT, RT.DIAGRAM_COLORS, RT.DIAGRAM_QUICK_STYLE}

# An embedded object is different, and treating it like a chart made whole templates unusable.
# Commercial decks are routinely built with add-ins - think-cell above all - that park an inert
# data blob on every slide ("think-cell data - do not delete"). Nothing on the slide reads it,
# nothing the engine does writes it, and sharing one such part between two slides is ordinary
# OOXML. Refusing to copy any slide carrying one meant refusing the entire template: on the
# first real one tried, every slide had it.
#
# So an embedded object no longer blocks copying. It is still never *edited* - `probe` marks it
# opaque - and `compile_from_kit` drops it from the copy, so a new deck does not inherit
# somebody else's add-in data.
_SHAREABLE = {RT.OLE_OBJECT, RT.PACKAGE}


def _sid(target: str) -> int:
    if not target.startswith("shape:"):
        raise RefError(f"step target must be 'shape:<id>', got {target!r}")
    return int(target.split(":")[1])


def _check_cloneable(slide):
    for rel in slide.part.rels.values():
        if rel.reltype in _NO_CLONE:
            raise RefError(f"slide {slide.slide_id} holds a chart/SmartArt/embedded object; "
                           "it can't be used as a pattern in this version")


def clone_slide(prs, src):
    """Append a copy of `src`: same layout, shapes, background and image links. Notes are not copied."""
    new = prs.slides.add_slide(src.slide_layout)
    tree = new.shapes._spTree
    for el in list(tree)[2:]:                      # drop placeholders the layout created
        tree.remove(el)
    for el in list(src.shapes._spTree)[2:]:        # keep nvGrpSpPr + grpSpPr, copy the rest
        tree.append(copy.deepcopy(el))
    bg = src._element.cSld.bg
    if bg is not None:
        new._element.cSld.insert(0, copy.deepcopy(bg))
    remap = {}
    for rId, rel in src.part.rels.items():
        if rel.reltype in (RT.NOTES_SLIDE, RT.SLIDE_LAYOUT):
            continue
        if rel.is_external:
            remap[rId] = new.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:                                      # images etc. are shared, not duplicated
            remap[rId] = new.part.relate_to(rel.target_part, rel.reltype)
    attrs = (qn("r:embed"), qn("r:id"), qn("r:link"))
    for el in new._element.iter():
        for a in attrs:
            if el.get(a) in remap:
                el.set(a, remap[el.get(a)])
    return new


def _xfrm(el):
    x = el.find(qn("p:xfrm"))                       # graphicFrame (tables)
    return x if x is not None else el.find(".//" + qn("a:xfrm"))


def _fill_paragraphs(txBody, texts):
    """Replace paragraphs; paragraph k copies the formatting of example paragraph k (or the last one)."""
    old = txBody.findall(qn("a:p"))
    for p in old:
        txBody.remove(p)
    for k, text in enumerate(texts):
        p = copy.deepcopy(old[min(k, len(old) - 1)])
        runs = [c for c in p if c.tag == qn("a:r")]
        for c in para_tokens(p):
            p.remove(c)
        if runs:
            r = runs[0]
        else:                                      # empty example paragraph: build a run from endParaRPr
            r = _new_run("")
            end = p.find(qn("a:endParaRPr"))
            if end is not None:
                rpr = copy.deepcopy(end)
                rpr.tag = qn("a:rPr")
                r.insert(0, rpr)
        r.find(qn("a:t")).text = text
        end = p.find(qn("a:endParaRPr"))
        if end is not None:
            end.addprevious(r)
        else:
            p.append(r)
        txBody.append(p)


def _write_geometry(el, spec):
    """Put a shape where the caller means, whatever space the shape's XML is written in.

    Positions arrive in slide coordinates, because that is the only space in which "put this
    box beside that one" means anything. A shape nested in a group stores its position in the
    group's own space, so it has to be converted back on the way in - see deckops/refs.py.
    """
    xfrm = el.find(".//" + qn("a:xfrm"))
    x, y, w, h = to_own(el, spec.x, spec.y, spec.w, spec.h)
    xfrm.find(qn("a:off")).set("x", str(x))
    xfrm.find(qn("a:off")).set("y", str(y))
    xfrm.find(qn("a:ext")).set("cx", str(w))
    xfrm.find(qn("a:ext")).set("cy", str(h))


def _apply_step(slide, st):
    tree = slide.shapes._spTree
    if isinstance(st, DuplicateShape):
        src = find_shape_el(slide, _sid(st.source))
        dup = copy.deepcopy(src)
        c = dup.find(".//" + qn("p:cNvPr"))
        c.set("id", str(st.new_id))
        c.set("name", f"{c.get('name', 'Shape')} copy {st.new_id}")
        # The copy is appended to the slide, not to whatever group the original sits in, so
        # its geometry has to be rewritten from the original's own space into the slide's -
        # otherwise a shape duplicated out of a group lands wherever those coordinates happen
        # to point, which is usually off the slide.
        x = _xfrm(dup) if dup.find(".//" + qn("a:xfrm")) is not None else None
        if x is not None:
            off, ext = x.find(qn("a:off")), x.find(qn("a:ext"))
            if off is not None and ext is not None:
                sx, sy, sw, sh = to_slide(src, int(off.get("x")), int(off.get("y")),
                                          int(ext.get("cx")), int(ext.get("cy")))
                off.set("x", str(sx)); off.set("y", str(sy))
                ext.set("cx", str(sw)); ext.set("cy", str(sh))
        tree.append(dup)
        return
    el = find_shape_el(slide, _sid(st.target))
    if isinstance(st, DeleteShape):
        el.getparent().remove(el)
    elif isinstance(st, SetGeometry):
        _write_geometry(el, st)
    elif isinstance(st, SetShapeText):
        _fill_paragraphs(el.find(qn("p:txBody")), st.paragraphs)
    elif isinstance(st, SetChart):
        _draw_chart(slide, el, st)
    elif isinstance(st, SetTable):
        tbl = el.find(".//" + qn("a:tbl"))
        trs = tbl.findall(qn("a:tr"))
        ncols = len(trs[0].findall(qn("a:tc")))
        if len(st.header) != ncols or any(len(r) != ncols for r in st.rows):
            raise RefError(f"table needs exactly {ncols} columns")
        head_tpl, body_tpls = trs[0], (trs[1:3] or trs[:1])
        for tr in trs:
            tbl.remove(tr)
        new_rows = [(head_tpl, st.header)] + [(body_tpls[i % len(body_tpls)], r) for i, r in enumerate(st.rows)]
        height = 0
        for tpl, cells in new_rows:
            tr = copy.deepcopy(tpl)
            for tc, text in zip(tr.findall(qn("a:tc")), cells):
                _fill_paragraphs(tc.find(qn("a:txBody")), [text])
            tbl.append(tr)
            height += int(tr.get("h", "0"))
        _xfrm(el).find(qn("a:ext")).set("cy", str(height))


def _draw_chart(slide, area_el, st):
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
    from pptx.util import Pt

    kinds = {"bar": XL_CHART_TYPE.BAR_CLUSTERED, "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
             "stacked_column": XL_CHART_TYPE.COLUMN_STACKED, "line": XL_CHART_TYPE.LINE_MARKERS,
             "pie": XL_CHART_TYPE.PIE, "doughnut": XL_CHART_TYPE.DOUGHNUT}
    off, ext = _xfrm(area_el).find(qn("a:off")), _xfrm(area_el).find(qn("a:ext"))
    x, y, w, h = (int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy")))
    area_el.getparent().remove(area_el)                      # the placeholder rectangle goes...
    data = CategoryChartData(number_format=st.number_format)
    data.categories = st.categories
    for s in st.series:
        data.add_series(s.name, s.values)
    chart = slide.shapes.add_chart(kinds[st.chart_type], x, y, w, h, data).chart   # ...a real chart takes its place
    rgb = RGBColor.from_string
    colours = [rgb(c) for c in st.palette]
    chart.font.name, chart.font.size = st.font, Pt(11)
    chart.font.color.rgb = rgb(st.text_colour)
    round_ = st.chart_type in ("pie", "doughnut")
    chart.has_title = bool(st.title)
    if st.title:
        chart.chart_title.text_frame.text = st.title
        r = chart.chart_title.text_frame.paragraphs[0].runs[0]
        r.font.size, r.font.bold, r.font.name = Pt(13), True, st.font
    chart.has_legend = round_ or len(st.series) > 1
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
    plot = chart.plots[0]
    for i, s in enumerate(plot.series):
        if round_:                                           # one colour per slice
            for j, pt in enumerate(s.points):
                pt.format.fill.solid()
                pt.format.fill.fore_color.rgb = colours[j % len(colours)]
        elif st.chart_type == "line":
            s.format.line.color.rgb = colours[i % len(colours)]
            s.format.line.width = Pt(2.25)
            s.smooth = False
            s.marker.format.fill.solid()
            s.marker.format.fill.fore_color.rgb = colours[i % len(colours)]
        else:
            s.format.fill.solid()
            s.format.fill.fore_color.rgb = colours[i % len(colours)]
    if not round_:
        plot.gap_width = 60
        va = chart.value_axis
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = rgb(st.grid_colour)
        va.format.line.fill.background()
        va.tick_labels.number_format, va.tick_labels.number_format_is_linked = st.number_format, False
        chart.category_axis.format.line.color.rgb = rgb(st.grid_colour)
        if st.chart_type == "bar":                           # horizontal bars: first category at the top
            chart.category_axis.reverse_order = True
    if st.show_values:
        plot.has_data_labels = True
        dl = plot.data_labels
        dl.font.size = Pt(10)
        if round_:
            dl.show_percentage, dl.show_value, dl.number_format = True, False, "0%"
            dl.number_format_is_linked = False
            if st.chart_type == "pie":
                dl.position = XL_LABEL_POSITION.OUTSIDE_END
            else:                                            # labels sit on the ring: white reads on every slice
                dl.font.color.rgb = rgb("FFFFFF")
                dl.font.bold = True
        else:
            dl.number_format, dl.number_format_is_linked = st.number_format, False
            if st.chart_type != "stacked_column":
                dl.position = XL_LABEL_POSITION.OUTSIDE_END


# ---- apply --------------------------------------------------------------------

def _new_run(text):
    r = OxmlElement("a:r")
    t = OxmlElement("a:t")
    t.text = text
    r.append(t)
    return r


def _apply_paragraph(p, value, warnings, target):
    tokens = parse_tagged(value)
    runs = para_runs(p)
    flds = [c for c in p if c.tag == qn("a:fld")]
    brs = [c for c in p if c.tag == qn("a:br")]
    if tokens[0][0] == "plain":
        text = tokens[0][1]
        if len(runs) > 1:
            warnings.append(f"{target}: untagged value collapsed {len(runs)} runs into one")
        tokens = [("r", 0, text)] if runs else [("new", text)]
    for c in para_tokens(p):
        p.remove(c)
    used, new = set(), []
    for t in tokens:
        if t[0] == "r":
            el = runs[t[1]] if t[1] not in used else copy.deepcopy(runs[t[1]])
            used.add(t[1])
            el.find(qn("a:t")).text = t[2]
        elif t[0] == "new":
            el = _new_run(t[1])
        elif t[0] == "br":
            el = copy.deepcopy(brs[0]) if brs else OxmlElement("a:br")
        else:
            el = flds[t[1]]
        new.append(el)
    end = p.find(qn("a:endParaRPr"))
    for el in new:
        if end is not None:
            end.addprevious(el)
        else:
            p.append(el)


def _apply(prs, op, b, warnings):
    if isinstance(op, SetParagraphText):
        _apply_paragraph(b["el"], op.value, warnings, op.target)
    elif isinstance(op, SetRunText):
        b["el"].find(qn("a:t")).text = op.value
    elif isinstance(op, SetFontSize):
        sz = str(int(round(op.size_pt * 100)))
        for r in b["el"].iter(qn("a:r")):
            r.get_or_add_rPr().set("sz", sz)
        for e in b["el"].iter(qn("a:endParaRPr")):
            e.set("sz", sz)
    elif isinstance(op, SetGeometry):
        _write_geometry(b["el"], op)
    elif isinstance(op, DeleteShape):
        b["el"].getparent().remove(b["el"])
    elif isinstance(op, DeleteSlide):
        el = b["sldId"]
        prs.part.drop_rel(el.get(qn("r:id")))
        el.getparent().remove(el)
    elif isinstance(op, SetDocProperties):
        import datetime
        cp = prs.core_properties
        cp.title, cp.author, cp.last_modified_by = op.title, op.author, op.author
        cp.subject = cp.keywords = cp.comments = cp.category = cp.content_status = ""
        cp.revision = 1
        cp.created = cp.modified = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    elif isinstance(op, AddSlideFromPattern):
        new = clone_slide(prs, b["slide"])
        for st in op.steps:
            _apply_step(new, st)
        if op.notes:
            new.notes_slide.notes_text_frame.text = op.notes
    elif isinstance(op, AddSlide):
        slide = prs.slides.add_slide(b["layout"])
        for ph in list(slide.placeholders):
            content = op.fill.get(ph.placeholder_format.idx)
            if content is None:
                if op.drop_empty_placeholders:
                    ph._element.getparent().remove(ph._element)
                continue
            tf = ph.text_frame
            items = [content.text] if content.kind == "text" else content.items
            tf.text = items[0]
            for it in items[1:]:
                tf.add_paragraph().text = it
        if op.notes:
            slide.notes_slide.notes_text_frame.text = op.notes
        if b["after"] is not None:
            new_el = prs.slides._sldIdLst[-1]
            b["after"].addnext(new_el)


# ---- save ---------------------------------------------------------------------

_PARSER = etree.XMLParser(remove_blank_text=True, resolve_entities=False, no_network=True)


def _canon(data: bytes):
    try:
        return etree.tostring(etree.fromstring(data, _PARSER), method="c14n")
    except etree.XMLSyntaxError:
        return None


_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _rels_meaning(name, data):
    """Relationship set with targets resolved, so '/ppt/x.xml' == '../x.xml'."""
    import posixpath
    base = posixpath.dirname(posixpath.dirname(name))          # ppt/slides/_rels/a.rels -> ppt/slides
    out = set()
    for r in etree.fromstring(data, _PARSER).iter(_RELS + "Relationship"):
        t, mode = r.get("Target"), r.get("TargetMode", "Internal")
        if mode == "Internal":
            t = posixpath.normpath(t.lstrip("/") if t.startswith("/") else posixpath.join(base, t))
        out.add((r.get("Id"), r.get("Type"), t, mode))
    return out


def _ct_meaning(data, part_names):
    """Content type each *existing* part resolves to; dangling entries don't count."""
    root = etree.fromstring(data, _PARSER)
    defaults = {e.get("Extension").lower(): e.get("ContentType") for e in root.iter(_CT + "Default")}
    overrides = {e.get("PartName"): e.get("ContentType") for e in root.iter(_CT + "Override")}
    return {n: overrides.get("/" + n) or defaults.get(n.rsplit(".", 1)[-1].lower()) for n in part_names}


def _equivalent(name, old, new, part_names):
    if old == new:
        return True
    try:
        if name == "[Content_Types].xml":
            return _ct_meaning(old, part_names) == _ct_meaning(new, part_names)
        if name.endswith(".rels"):
            return _rels_meaning(name, old) == _rels_meaning(name, new)
        if name.endswith(".xml"):
            return _canon(old) == _canon(new)
    except etree.XMLSyntaxError:
        pass
    return False


def save_preserving(prs, src: str, dst: str) -> dict:
    tmp = dst + ".tmp"
    prs.save(tmp)
    stats = {"preserved": [], "changed": [], "added": [], "removed": []}
    with zipfile.ZipFile(src) as zs, zipfile.ZipFile(tmp) as zn, \
            zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
        old_names = {n for n in zs.namelist() if not n.endswith("/")}   # ignore folder entries
        new_parts = [n for n in zn.namelist() if not n.endswith("/") and n != "[Content_Types].xml"]
        for name in zn.namelist():
            new = zn.read(name)
            if name not in old_names:
                stats["added"].append(name)
                out.writestr(name, new)
                continue
            old = zs.read(name)
            same = _equivalent(name, old, new, new_parts)
            stats["preserved" if same else "changed"].append(name)
            out.writestr(name, old if same else new)
        stats["removed"] = sorted(old_names - set(zn.namelist()))
    os.remove(tmp)
    return stats


# ---- entry point ----------------------------------------------------------------

def execute(src: str, ops_json: list, dst: str) -> dict:
    ops = OpList.validate_python(ops_json)
    prs = Presentation(src)
    bound, errors = [], []
    for i, op in enumerate(ops):
        try:
            bound.append((i, op, _bind(prs, op)))
        except (RefError, TagError) as e:
            errors.append((i, str(e)))
    if errors:
        raise ExecError(errors)
    warnings = []
    for i, op, b in sorted(bound, key=lambda t: (PHASE[type(t[1])], t[0])):
        _apply(prs, op, b, warnings)
    stats = save_preserving(prs, src, dst)
    return {"ops_applied": len(ops), "warnings": warnings, "parts": {k: (v if k != "preserved" else len(v)) for k, v in stats.items()}}
