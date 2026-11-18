"""The slide pieces: each draws one kind of visual into a box, in the template's colours.

A piece is `piece(slide, box, data, profile) -> list[str]`: it draws into `box` (inches) and
returns problems it could not solve itself (text that would not fit even at the smallest size
allowed), which the renderer reports instead of drawing something broken.

Rules every piece keeps, so slides look designed rather than generated:
  * text is measured before it is drawn (design.measure) and is never smaller than the
    template's small size; things of the same kind share one size (every KPI value, every
    card heading)
  * colours only from the profile, chosen by role ("primary", "accent", "alert", ...)
  * no shadows, no gradients, no clip art: flat fills, thin lines, generous space

Called by: design.render.render_slide, through PIECES.
"""
from __future__ import annotations

from dataclasses import dataclass

from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION, XL_TICK_MARK
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

from . import measure

EMU = 914400


@dataclass(frozen=True)
class Box:
    """A rectangle on the slide, in inches."""
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def inset(self, dx: float, dy: float | None = None) -> "Box":
        """This box shrunk by dx on the left and right and dy on the top and bottom."""
        dy = dx if dy is None else dy
        return Box(self.x + dx, self.y + dy, self.w - 2 * dx, self.h - 2 * dy)

    def cols(self, n: int, gap: float) -> list["Box"]:
        """n equal columns, `gap` apart."""
        w = (self.w - gap * (n - 1)) / n
        return [Box(self.x + i * (w + gap), self.y, w, self.h) for i in range(n)]

    def take_top(self, h: float, gap: float = 0.0) -> tuple["Box", "Box"]:
        """(the top `h` of this box, what is left below it after `gap`)."""
        return Box(self.x, self.y, self.w, h), Box(self.x, self.y + h + gap, self.w, self.h - h - gap)

    def take_bottom(self, h: float, gap: float = 0.0) -> tuple["Box", "Box"]:
        """(the bottom `h` of this box, what is left above it after `gap`)."""
        return (Box(self.x, self.bottom - h, self.w, h),
                Box(self.x, self.y, self.w, self.h - h - gap))


# --------------------------------------------------------------------------- drawing helpers

def _rgb(hex6: str) -> RGBColor:
    return RGBColor.from_string(hex6)


def colour(P: dict, role: str | None, default: str = "primary") -> str:
    """A profile colour by role ("primary", "accent", "alert", "primary_dark", ...)."""
    c = P["colours"]
    return c.get(role or default) or c[default]


def rect(slide, box: Box, fill: str | None, line: str | None = None, line_pt: float = 0.75,
         shape=MSO_SHAPE.RECTANGLE, radius: float | None = None):
    """A flat shape: solid fill (or none), an optional thin outline, never a shadow."""
    sh = slide.shapes.add_shape(shape, Emu(int(box.x * EMU)), Emu(int(box.y * EMU)),
                                Emu(int(box.w * EMU)), Emu(int(box.h * EMU)))
    sh.shadow.inherit = False
    if fill:
        sh.fill.solid()
        sh.fill.fore_color.rgb = _rgb(fill)
    else:
        sh.fill.background()
    if line:
        sh.line.color.rgb = _rgb(line)
        sh.line.width = Pt(line_pt)
    else:
        sh.line.fill.background()
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        sh.adjustments[0] = min(0.5, radius / min(box.w, box.h))
    return sh


def text(slide, box: Box, value: str, pt: float, colour_hex: str, P: dict, bold: bool = False,
         italic: bool = False, align: str = "left", anchor: str = "top", shape=None,
         spacing: float = 1.0):
    """A text box (or the text of `shape`) with no insets, no autofit, wrapping on.

    The size was chosen by the caller with design.measure, so what is drawn is what was measured.
    """
    if shape is None:
        shape = slide.shapes.add_textbox(Emu(int(box.x * EMU)), Emu(int(box.y * EMU)),
                                         Emu(int(box.w * EMU)), Emu(int(box.h * EMU)))
    tf = shape.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE,
                          "bottom": MSO_ANCHOR.BOTTOM}[anchor]
    for i, para in enumerate(value.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER,
                       "right": PP_ALIGN.RIGHT}[align]
        if spacing != 1.0:
            p.line_spacing = spacing
        r = p.add_run()
        r.text = para
        f = r.font
        f.size, f.bold, f.italic, f.name = Pt(pt), bold, italic, P["font"]
        f.color.rgb = _rgb(colour_hex)
    return shape


def arrowhead(connector, at: str = "end", size: str = "med") -> None:
    """Put a triangle arrowhead on a line: at its "end" (x2, y2), its "start", or "both".

    python-pptx has no setting for this; it is the a:tailEnd / a:headEnd element of the
    line's a:ln. Calling it twice does not stack heads.
    """
    ln = connector.line._get_or_add_ln()
    tags = {"end": ["a:tailEnd"], "start": ["a:headEnd"], "both": ["a:headEnd", "a:tailEnd"]}[at]
    for tag in tags:
        for old in ln.findall(qn(tag)):
            ln.remove(old)
        el = ln.makeelement(qn(tag), {"type": "triangle", "w": size, "len": size})
        # schema order inside a:ln: fills, prstDash, join, headEnd, tailEnd, extLst
        ext = ln.find(qn("a:extLst"))
        if tag == "a:headEnd" and ln.find(qn("a:tailEnd")) is not None:
            ln.find(qn("a:tailEnd")).addprevious(el)
        elif ext is not None:
            ext.addprevious(el)
        else:
            ln.append(el)


def line(slide, x1: float, y1: float, x2: float, y2: float, colour_hex: str, pt: float = 1.0,
         arrow: str | None = None, head: str = "med"):
    """A straight connector from (x1, y1) to (x2, y2); arrow: None, "end", "start" or "both"."""
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(int(x1 * EMU)), Emu(int(y1 * EMU)),
                                   Emu(int(x2 * EMU)), Emu(int(y2 * EMU)))
    c.line.color.rgb = _rgb(colour_hex)
    c.line.width = Pt(pt)
    if arrow:
        arrowhead(c, arrow, head)
    return c


# --------------------------------------------------------------------------- pieces

def _too_long(piece: str, texts: list[str], w: float, h: float, P: dict) -> str:
    """A fit failure the writer can act on: which entries are too long, and the word budget."""
    budget = measure.words_that_fit(w, h, P["small_pt"], P["font"])
    long = [(i + 1, len(t.split())) for i, t in enumerate(texts)
            if t and not measure.fits(t, P["small_pt"], w, h, P["font"])]
    if long and all(n <= budget for _, n in long) or budget < 8:
        # The words are few; the space is the problem. Saying "shorter" would send the writer
        # the wrong way.
        return (f"{piece}: its row is too short for its text ({h:.1f} inch of text space); give "
                "that row more weight, drop another row, or use fewer items")
    which = ", ".join(f"entry {i} has {n} words" for i, n in long) or "the entries together are too long"
    return f"{piece}: text does not fit ({which}); keep each to at most {budget} words"


def kpis(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A row of headline numbers: card, coloured top bar, big value, short label.

    d: {"items": [{"value": "20/hr", "label": "Current throughput ceiling",
                   "tone": "primary|accent|alert|primary_dark"}]}
    """
    items = d["items"]
    cards = box.cols(len(items), 0.22)
    pad, bar = 0.22, 0.09
    inner_w = cards[0].w - 2 * pad
    body = cards[0].h - bar - 2 * pad
    value_h, label_h = body * 0.58, body * 0.42 - 0.08
    vpt = measure.fit_all([i["value"] for i in items], inner_w, value_h, 60, 24,
                          family=P["font"], bold=True, max_lines=1)
    lpt = measure.fit_all([i.get("label", "") for i in items], inner_w, label_h,
                          P["body_pt"] + 1, P["small_pt"], family=P["font"], max_lines=2)
    problems = []
    if vpt is None:
        problems.append("kpis: a value is too long for its card; keep values under ~7 characters")
        vpt = 24
    if lpt is None:
        problems.append("kpis: a label is too long; keep labels to a few words")
        lpt = P["small_pt"]
    for k, (card, item) in enumerate(zip(cards, items)):
        tone = colour(P, item.get("tone"), "primary") if item.get("tone") else _cycle(P, k)
        if filled(P):                      # a solid tile in its colour, the number in white
            rect(slide, card, tone)
            value_colour, label_colour = "FFFFFF", "FFFFFF"
        else:
            rect(slide, card, P["colours"]["card"], P["colours"]["line"])
            rect(slide, Box(card.x, card.y, card.w, bar), tone)
            value_colour, label_colour = tone, P["colours"]["muted"]
        inner = card.inset(pad)
        v = Box(inner.x, card.y + bar + pad, inner.w, value_h)
        text(slide, v, item["value"], vpt, value_colour, P, bold=True, align="center", anchor="bottom")
        lab = Box(inner.x, v.bottom + 0.08, inner.w, label_h)
        text(slide, lab, item.get("label", ""), lpt, label_colour, P,
             align="center", anchor="top")
    return problems


_CHARTS = {"column": XL_CHART_TYPE.COLUMN_CLUSTERED, "bar": XL_CHART_TYPE.BAR_CLUSTERED,
           "line": XL_CHART_TYPE.LINE_MARKERS, "stacked": XL_CHART_TYPE.COLUMN_STACKED,
           "doughnut": XL_CHART_TYPE.DOUGHNUT}


def chart(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A native PowerPoint chart in the brand colours, labelled with its values, no clutter.

    d: {"kind": "column|bar|line|stacked|doughnut", "heading": "...",
        "categories": [...], "series": [{"name": "...", "values": [...]}],
        "highlight": [<category indices drawn in the accent colour>],
        "number_format": "0" | "0%" | "#,##0" ...}
    """
    problems = []
    if d.get("heading"):
        hpt = P["body_pt"] + 2
        hbox, box = box.take_top(measure.height_in(1, hpt) + 0.02, gap=0.1)
        text(slide, hbox, d["heading"], hpt, P["colours"]["text"], P, bold=True)
    data = CategoryChartData()
    data.categories = d["categories"]
    for s in d["series"]:
        data.add_series(s["name"], s["values"])
    kind = d.get("kind", "column")
    gf = slide.shapes.add_chart(_CHARTS[kind], Emu(int(box.x * EMU)), Emu(int(box.y * EMU)),
                                Emu(int(box.w * EMU)), Emu(int(box.h * EMU)), data)
    ch = gf.chart
    ch.has_title = False                  # the heading above says what the chart is
    ch.font.name, ch.font.size = P["font"], Pt(P["small_pt"])
    ch.font.color.rgb = _rgb(P["colours"]["muted"])
    multi = len(d["series"]) > 1
    ch.has_legend = multi or kind == "doughnut"
    if ch.has_legend:
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM
        ch.legend.include_in_layout = False
    plot = ch.plots[0]
    plot.has_data_labels = True
    dl = plot.data_labels
    dl.font.size, dl.font.bold = Pt(P["body_pt"]), True
    dl.font.color.rgb = _rgb(P["colours"]["text"])
    dl.number_format = d.get("number_format", "General")
    dl.number_format_is_linked = False
    tones = [P["colours"]["primary"], P["colours"]["accent"], P["colours"]["primary_dark"],
             P["colours"]["accent_dark"]]
    if kind == "doughnut":
        for i, pt in enumerate(ch.plots[0].series[0].points):
            pt.format.fill.solid()
            pt.format.fill.fore_color.rgb = _rgb(tones[i % len(tones)])
        return problems
    if kind in ("column", "bar", "stacked"):
        plot.gap_width = 60
        if kind != "stacked":
            dl.position = XL_LABEL_POSITION.OUTSIDE_END
        plot.overlap = 100 if kind == "stacked" else -10
    for i, s in enumerate(plot.series):
        tone = tones[i % len(tones)]
        if kind == "line":
            s.format.line.color.rgb = _rgb(tone)
            s.format.line.width = Pt(2.5)
            s.smooth = False
            s.marker.format.fill.solid()
            s.marker.format.fill.fore_color.rgb = _rgb(tone)
            s.marker.format.line.color.rgb = _rgb(tone)
        else:
            s.format.fill.solid()
            s.format.fill.fore_color.rgb = _rgb(tone)
    if kind == "line" and (multi or len(d["categories"]) > 8):
        # A label on every point of several lines is noise: label only where each line ends.
        # One short line keeps a label on every point - otherwise it has no numbers at all.
        plot.has_data_labels = False
        for s in plot.series:
            end = s.points[len(d["categories"]) - 1].data_label
            end.has_text_frame = False
            end.show_value = True
            end.position = XL_LABEL_POSITION.ABOVE
            end.font.size, end.font.bold = Pt(P["body_pt"]), True
            end.font.color.rgb = _rgb(P["colours"]["text"])
    if not multi and kind != "line":
        for i in d.get("highlight", []):
            p = plot.series[0].points[i]
            p.format.fill.solid()
            p.format.fill.fore_color.rgb = _rgb(P["colours"]["accent"])
    if kind == "line" and not (multi or len(d["categories"]) > 8):
        dl.position = XL_LABEL_POSITION.ABOVE
    va, ca = ch.value_axis, ch.category_axis
    # The value scale is hidden by default (the data labels carry the numbers); a spec can ask
    # for it, with light gridlines, and for axis titles.
    va.visible = bool(d.get("value_axis"))
    va.has_major_gridlines = bool(d.get("value_axis"))
    if va.has_major_gridlines:
        va.major_gridlines.format.line.color.rgb = _rgb(P["colours"]["line"])
        va.tick_labels.font.size = Pt(P["small_pt"])
        va.format.line.fill.background()
    va.has_minor_gridlines = False
    for axis, key in ((ca, "x_title"), (va, "y_title")):
        if d.get(key):
            axis.has_title = True
            axis.axis_title.text_frame.text = d[key]
            r = axis.axis_title.text_frame.paragraphs[0].runs[0]
            r.font.size, r.font.bold = Pt(P["small_pt"]), False
            r.font.color.rgb = _rgb(P["colours"]["muted"])
            if key == "y_title":
                va.visible = True
    if d.get("max") is not None:
        va.maximum_scale = d["max"]
    va.minimum_scale = 0
    ca.tick_labels.font.size = Pt(P["body_pt"])
    ca.tick_labels.font.color.rgb = _rgb(P["colours"]["text"])
    ca.major_tick_mark = XL_TICK_MARK.NONE
    ca.format.line.color.rgb = _rgb(P["colours"]["line"])
    return problems


def steps(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A numbered sequence left to right: numbered circles on a rule, a card under each.

    d: {"items": [{"heading": "...", "text": "..."}], "last_is_outcome": true|false}
    The last circle is drawn in the accent colour when last_is_outcome is set.
    """
    items = d["items"]
    n = len(items)
    diam = 0.56
    cols = box.cols(n, 0.25)
    top_y = box.y
    card_top = top_y + diam + 0.22
    pad = 0.24
    inner_w = cols[0].w - 2 * pad
    card_h = box.bottom - card_top
    # Headings: every one on a single line if that still reads as a heading, two lines if not.
    heads = [i["heading"] for i in items]
    hpt = measure.fit_all(heads, inner_w, measure.height_in(1, P["body_pt"] + 4), P["body_pt"] + 4,
                          P["body_pt"] + 1, family=P["font"], bold=True, max_lines=1)
    if hpt is None:
        hpt = measure.fit_all(heads, inner_w, measure.height_in(2, P["body_pt"] + 4),
                              P["body_pt"] + 4, P["body_pt"], family=P["font"], bold=True,
                              max_lines=2)
    head_h = measure.height_in(2, P["body_pt"] + 4)
    body_h = card_h - 2 * pad - 0.2 - head_h - 0.12
    bpt = measure.fit_all([i.get("text", "") for i in items], inner_w, body_h, P["body_pt"] + 2,
                          P["small_pt"], family=P["font"], spacing=1.05)
    problems = []
    if hpt is None:
        problems.append("steps: a heading is too long; keep headings to 2-4 words")
        hpt = P["body_pt"]
    if bpt is None:
        problems.append(_too_long("steps", [i.get("text", "") for i in items], inner_w, body_h, P))
        bpt = P["small_pt"]
    # Headings take only the lines they need (the same for every card, so the texts line up).
    head_lines = max(len(measure.wrap(i["heading"], hpt, inner_w, P["font"], True)) for i in items)
    head_h = measure.height_in(head_lines, hpt)
    # Cards as tall as their longest text, not as the row: the spare height goes above and
    # below the whole piece instead of sitting empty inside every card.
    text_lines = max(len(measure.wrap(i.get("text", ""), bpt, inner_w, P["font"])) for i in items)
    need = pad + 0.2 + head_h + 0.12 + measure.height_in(text_lines, bpt, 1.05) + pad + 0.06
    if need < card_h:
        shift = (card_h - need) / 2
        top_y, card_top, card_h = top_y + shift, card_top + shift, need
    cy = top_y + diam / 2
    # One arrow from each circle to the next, so the order reads as a flow.
    gap = 0.08
    for a, b in zip(cols, cols[1:]):
        line(slide, a.x + a.w / 2 + diam / 2 + gap, cy, b.x + b.w / 2 - diam / 2 - gap, cy,
             P["colours"]["muted"], 1.5, arrow="end", head="lg")
    for k, (col, item) in enumerate(zip(cols, items)):
        last = d.get("last_is_outcome") and k == n - 1
        tone = P["colours"]["accent"] if last else _cycle(P, k)
        circ = rect(slide, Box(col.x + col.w / 2 - diam / 2, top_y, diam, diam), tone,
                    shape=MSO_SHAPE.OVAL)
        text(slide, None, str(k + 1), P["body_pt"] + 4, "FFFFFF", P, bold=True, align="center",
             anchor="middle", shape=circ)
        card = Box(col.x, card_top, col.w, card_h)
        rect(slide, card, P["colours"]["card"], P["colours"]["line"],
             shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
        rect(slide, Box(card.x + pad, card.y + pad, 0.5, 0.06), tone)
        inner = card.inset(pad)
        hb = Box(inner.x, inner.y + 0.2, inner.w, head_h)
        text(slide, hb, item["heading"], hpt, P["colours"]["primary_dark"], P, bold=True)
        tb = Box(inner.x, hb.bottom + 0.12, inner.w, inner.bottom - hb.bottom - 0.12)
        text(slide, tb, item.get("text", ""), bpt, P["colours"]["text"], P, spacing=1.05)
    return problems


def callout(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A full-width band carrying the slide's bottom line.

    d: {"label": "Bottom line", "text": "...", "tone": "primary_dark|accent"}
    """
    tone = colour(P, d.get("tone"), "primary_dark")
    rect(slide, box, tone)
    pad_x = 0.3
    inner = box.inset(pad_x, 0.12)
    problems = []
    if d.get("label"):
        lpt = P["body_pt"] + 1
        lw = measure.width_in(d["label"], lpt, P["font"], True) + 0.05
        text(slide, Box(inner.x, inner.y, lw, inner.h), d["label"], lpt,
             P["colours"]["accent_light"] if tone != P["colours"]["accent"] else "FFFFFF", P,
             bold=True, anchor="middle")
        rule_x = inner.x + lw + 0.22
        line(slide, rule_x, inner.y + 0.06, rule_x, inner.bottom - 0.06, "FFFFFF", 1.0)
        inner = Box(rule_x + 0.25, inner.y, inner.right - rule_x - 0.25, inner.h)
    tpt = measure.fit(d["text"], inner.w, inner.h, P["body_pt"] + 3, P["small_pt"],
                      family=P["font"], bold=True, max_lines=2)
    if tpt is None:
        problems.append("callout: the text is too long for one band; keep it to one sentence")
        tpt = P["small_pt"]
    text(slide, inner, d["text"], tpt, "FFFFFF", P, bold=True, anchor="middle")
    return problems


def footnote(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A small note under the content: assumptions, sources. d: {"text": "..."}"""
    pt = P["small_pt"] - 1
    text(slide, box, d["text"], pt, P["colours"]["muted"], P, italic=True, anchor="bottom")
    return []


def filled(P: dict) -> bool:
    """Whether the deck uses the extended palette's filled, more colourful styles."""
    return P.get("palette") == "extended"


def _tones(P: dict) -> list[str]:
    """The colours a set of side-by-side things takes, in order. With the extended palette, the
    brand's two colours lead and the derived hues follow."""
    c = P["colours"]
    if filled(P):
        return [c["primary"], c["teal"], c["accent"], c["blue"], c["plum"], c["amber"]]
    return [c["primary"], c["accent"], c["primary_dark"], c["accent_dark"]]


def _cycle(P: dict, k: int, default: str = "primary") -> str:
    """The k-th thing's colour: one colour for all in the brand palette, a cycle in the extended."""
    return _tones(P)[k % len(_tones(P))] if filled(P) else P["colours"][default]


def _light(P: dict, tone: str) -> str:
    """The pale tint that goes with a tone, for panels behind text."""
    c = P["colours"]
    if tone in (c["accent"], c["accent_dark"]):
        return c["accent_light"]
    if tone in (c["primary"], c["primary_dark"]):
        return c["primary_light"]
    for name, value in c.items():
        if value == tone and f"{name}_light" in c:
            return c[f"{name}_light"]
    return c["panel"]


def _spread(heights: list[float], box: Box, max_gap: float) -> tuple[float, float]:
    """(first y, gap) placing blocks of these heights down `box`: evenly spread up to max_gap,
    then top-aligned."""
    n = len(heights)
    spare = box.h - sum(heights)
    gap = min(max_gap, spare / (n - 1)) if n > 1 else 0
    return box.y, max(gap, 0.0)


def points(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A vertical list of points, each a bold heading over a line or two of text.

    d: {"label": "optional heading over the whole list", "items": [{"heading": "...", "text": "..."}],
        "numbered": true|false, "tone": "..."}
    """
    items = d["items"]
    n = len(items)
    tone = colour(P, d.get("tone"), "primary")
    if d.get("label"):
        lpt = P["body_pt"] + 4
        lab, box = box.take_top(measure.height_in(1, lpt) + 0.12, gap=0.22)
        text(slide, Box(lab.x, lab.y, lab.w, lab.h - 0.1), d["label"], lpt, P["colours"]["primary_dark"], P, bold=True)
        rect(slide, Box(lab.x, lab.bottom - 0.05, min(lab.w, 1.2), 0.05), tone)
    numbered = d.get("numbered")
    mark_w = 0.62 if numbered else 0.3
    w = box.w - mark_w
    row_h = (box.h - 0.18 * (n - 1)) / n
    has_heads = any(i.get("heading") for i in items)
    hpt = measure.fit_all([i.get("heading", "") for i in items], w, measure.height_in(1, P["body_pt"] + 4),
                          P["body_pt"] + 4, P["body_pt"], family=P["font"], bold=True, max_lines=1) \
        if has_heads else 0
    head_h = measure.height_in(1, hpt) + 0.05 if has_heads and hpt else 0
    tpt = measure.fit_all([i.get("text", "") for i in items], w, row_h - head_h, P["body_pt"] + 3,
                          P["small_pt"], family=P["font"])
    problems = []
    if has_heads and not hpt:
        problems.append("points: a heading is too long for one line; keep headings to a few words")
        hpt, head_h = P["body_pt"], measure.height_in(1, P["body_pt"]) + 0.05
    if tpt is None:
        problems.append(_too_long("points", [i.get("text", "") for i in items], w, row_h - head_h, P))
        tpt = P["small_pt"]
    heights = [head_h * bool(i.get("heading")) +
               measure.height_in(len(measure.wrap(i.get("text", ""), tpt, w, P["font"])) if i.get("text") else 0, tpt)
               for i in items]
    y, gap = _spread(heights, box, 0.42)
    if not d.get("label"):              # a labelled list starts under its label; others are centred
        y += max(0.0, (box.h - sum(heights) - gap * (n - 1)) / 2)
    for k, (item, h) in enumerate(zip(items, heights)):
        if numbered:
            circ = rect(slide, Box(box.x, y, 0.44, 0.44), tone, shape=MSO_SHAPE.OVAL)
            text(slide, None, str(k + 1), P["body_pt"] + 1, "FFFFFF", P, bold=True,
                 align="center", anchor="middle", shape=circ)
        else:
            rect(slide, Box(box.x, y + 0.04, 0.09, max(h - 0.08, 0.2)),
                 _cycle(P, k) if filled(P) and not d.get("tone") else tone)
        x = box.x + mark_w
        yy = y
        if item.get("heading"):
            text(slide, Box(x, yy, w, head_h), item["heading"], hpt, P["colours"]["primary_dark"], P, bold=True)
            yy += head_h
        if item.get("text"):
            text(slide, Box(x, yy, w, h - (yy - y) + 0.02), item["text"], tpt, P["colours"]["text"], P)
        if k < n - 1 and gap > 0.2:
            line(slide, x, y + h + gap / 2, box.right, y + h + gap / 2, P["colours"]["line"], 0.75)
        y += h + gap
    return problems


def cards(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A grid of cards: optional big value, a heading, a short text. Up to 6, in 2-4 columns.

    d: {"items": [{"value": "38%", "heading": "...", "text": "..."}], "columns": 3}
    """
    items = d["items"]
    n = len(items)
    ncols = d.get("columns") or (n if n <= 4 else 3)
    nrows = -(-n // ncols)
    gap, pad, bar = 0.22, 0.22, 0.07
    cw = (box.w - gap * (ncols - 1)) / ncols
    ch = (box.h - gap * (nrows - 1)) / nrows
    iw = cw - 2 * pad
    has_val = any(i.get("value") for i in items)
    vpt = measure.fit_all([i.get("value", "") for i in items], iw, measure.height_in(1, 54), 54, 24,
                          family=P["font"], bold=True, max_lines=1) if has_val else 0
    val_h = measure.height_in(1, vpt) + 0.06 if has_val else 0
    hpt = measure.fit_all([i["heading"] for i in items], iw, measure.height_in(2, P["body_pt"] + 4),
                          P["body_pt"] + 4, P["body_pt"], family=P["font"], bold=True, max_lines=2)
    problems = []
    if hpt is None:
        problems.append("cards: a heading is too long; keep card headings to 2-5 words")
        hpt = P["body_pt"]
    head_lines = max(len(measure.wrap(i["heading"], hpt, iw, P["font"], True)) for i in items)
    head_h = measure.height_in(head_lines, hpt)
    room = ch - 2 * pad - bar - val_h - head_h - 0.1
    tpt = measure.fit_all([i.get("text", "") for i in items], iw, room, P["body_pt"] + 2,
                          P["small_pt"], family=P["font"])
    if tpt is None:
        problems.append(_too_long("cards", [i.get("text", "") for i in items], iw, room, P))
        tpt = P["small_pt"]
    text_lines = max((len(measure.wrap(i.get("text", ""), tpt, iw, P["font"])) for i in items if i.get("text")),
                     default=0)
    need = bar + 2 * pad + val_h + head_h + 0.1 + measure.height_in(text_lines, tpt) + 0.04
    card_h = min(ch, need)
    top = box.y + (box.h - (card_h * nrows + gap * (nrows - 1))) / 2
    tones = _tones(P)
    for k, item in enumerate(items):
        r, c = divmod(k, ncols)
        # a short last row is centred, not left hanging
        in_row = min(ncols, n - r * ncols)
        x0 = box.x + (box.w - (in_row * cw + (in_row - 1) * gap)) / 2
        card = Box(x0 + c * (cw + gap), top + r * (card_h + gap), cw, card_h)
        tone = colour(P, item.get("tone"), "primary") if item.get("tone") else (
            _cycle(P, k) if filled(P) else tones[0])
        if filled(P):                      # a tinted card under a heavier bar in its colour
            rect(slide, card, _light(P, tone))
            rect(slide, Box(card.x, card.y, card.w, bar + 0.04), tone)
        else:
            rect(slide, card, P["colours"]["card"], P["colours"]["line"])
            rect(slide, Box(card.x, card.y, card.w, bar), tone)
        y = card.y + bar + pad
        if has_val and item.get("value"):
            text(slide, Box(card.x + pad, y, iw, val_h), item["value"], vpt, tone, P, bold=True)
        y += val_h
        text(slide, Box(card.x + pad, y, iw, head_h), item["heading"], hpt, P["colours"]["primary_dark"], P, bold=True)
        y += head_h + 0.1
        if item.get("text"):
            text(slide, Box(card.x + pad, y, iw, card.bottom - pad - y + 0.04), item["text"], tpt,
                 P["colours"]["text"], P)
    return problems


def compare(slide, box: Box, d: dict, P: dict) -> list[str]:
    """Two or three columns side by side, each under a coloured label: options, before/after.

    d: {"columns": [{"label": "Today", "tone": "primary", "points": ["...", "..."]}]}
    """
    cols = d["columns"]
    boxes = box.cols(len(cols), 0.3)
    head_h, pad, dot = 0.56, 0.26, 0.11
    iw = boxes[0].w - 2 * pad - dot - 0.18
    lpt = measure.fit_all([c["label"] for c in cols], boxes[0].w - 0.4, head_h - 0.1, P["body_pt"] + 4,
                          P["body_pt"], family=P["font"], bold=True, max_lines=1)
    most = max(len(c["points"]) for c in cols)
    every = [p for c in cols for p in c["points"]]
    room = (box.h - head_h - 2 * pad - 0.18 * (most - 1)) / most
    tpt = measure.fit_all(every, iw, room, P["body_pt"] + 4, P["small_pt"], family=P["font"])
    problems = []
    if lpt is None:
        problems.append("compare: a column label is too long; use 1-3 words")
        lpt = P["body_pt"]
    if tpt is None:
        problems.append(_too_long("compare", every, iw, room, P) + "; or use fewer points")
        tpt = P["small_pt"]
    # Panels as tall as the fullest column (points spread a little when there is room), and the
    # whole piece centred in its cell, rather than tall tinted panels with the text at the top.
    col_h = [[measure.height_in(len(measure.wrap(p, tpt, iw, P["font"])), tpt) for p in c["points"]]
             for c in cols]
    gap = 0.18
    fullest = max(sum(h) + gap * (len(h) - 1) for h in col_h)
    gap = min(0.4, gap + max(0.0, box.h - head_h - 2 * pad - fullest) / max(1, most - 1) / 2)
    panel_h = min(box.h - head_h, max(sum(h) + gap * (len(h) - 1) for h in col_h) + 2 * pad)
    top = box.y + (box.h - head_h - panel_h) / 2
    tones = _tones(P)
    for k, (bx, col, hs) in enumerate(zip(boxes, cols, col_h)):
        tone = colour(P, col.get("tone"), "primary") if col.get("tone") else tones[k % len(tones)]
        head = rect(slide, Box(bx.x, top, bx.w, head_h), tone)
        text(slide, None, col["label"], lpt, "FFFFFF", P, bold=True, align="center", anchor="middle", shape=head)
        panel = Box(bx.x, top + head_h, bx.w, panel_h)
        rect(slide, panel, _light(P, tone))
        y = panel.y + pad
        for p, h in zip(col["points"], hs):
            first = measure.height_in(1, tpt)
            rect(slide, Box(panel.x + pad, y + first / 2 - dot / 2, dot, dot), tone, shape=MSO_SHAPE.OVAL)
            text(slide, Box(panel.x + pad + dot + 0.18, y, iw, h + 0.02), p, tpt, P["colours"]["text"], P)
            y += h + gap
    return problems


def timeline(slide, box: Box, d: dict, P: dict) -> list[str]:
    """Dated milestones along a line: the date above, what happens below.

    d: {"items": [{"when": "Q1 2026", "heading": "...", "text": "..."}], "last_is_outcome": bool}
    """
    items = d["items"]
    n = len(items)
    cols = box.cols(n, 0.25)
    iw = cols[0].w - 0.1
    wpt = measure.fit_all([i["when"] for i in items], iw, measure.height_in(1, 22), 22, P["body_pt"],
                          family=P["font"], bold=True, max_lines=1)
    hpt = measure.fit_all([i["heading"] for i in items], iw, measure.height_in(2, P["body_pt"] + 4),
                          P["body_pt"] + 4, P["body_pt"], family=P["font"], bold=True, max_lines=2)
    problems = []
    if wpt is None:
        problems.append("timeline: a date label is too long; use e.g. 'Q3 2026' or 'Week 4'")
        wpt = P["body_pt"]
    if hpt is None:
        problems.append("timeline: a heading is too long; keep it to 2-4 words")
        hpt = P["body_pt"]
    when_h = measure.height_in(1, wpt)
    head_lines = max(len(measure.wrap(i["heading"], hpt, iw, P["font"], True)) for i in items)
    head_h = measure.height_in(head_lines, hpt)
    room = box.h - (when_h + 0.3 + 0.35 + head_h + 0.1)
    tpt = measure.fit_all([i.get("text", "") for i in items], iw, room, P["body_pt"] + 3, P["small_pt"],
                          family=P["font"])
    if tpt is None:
        problems.append(_too_long("timeline", [i.get("text", "") for i in items], iw, room, P))
        tpt = P["small_pt"]
    text_h = max((measure.height_in(len(measure.wrap(i.get("text", ""), tpt, iw, P["font"])), tpt)
                  for i in items if i.get("text")), default=0)
    total = when_h + 0.3 + 0.35 + head_h + 0.1 + text_h
    top = box.y + max(0.0, (box.h - total) / 2)               # centred in its cell
    line_y = top + when_h + 0.3
    x0, x1 = cols[0].x + 0.13, cols[-1].right
    line(slide, x0, line_y, x1, line_y, P["colours"]["line"], 2.0)
    # Time runs on: a darker tip on the last stretch, so the head shows against the light track.
    line(slide, x1 - 0.45, line_y, x1, line_y, P["colours"]["muted"], 2.0, arrow="end", head="lg")
    for k, (col, item) in enumerate(zip(cols, items)):
        last = d.get("last_is_outcome") and k == n - 1
        tone = P["colours"]["accent"] if last else _cycle(P, k)
        text(slide, Box(col.x, top, iw, when_h + 0.02), item["when"], wpt, tone, P, bold=True)
        rect(slide, Box(col.x, line_y - 0.13, 0.26, 0.26), tone, shape=MSO_SHAPE.OVAL)
        y = line_y + 0.35
        text(slide, Box(col.x, y, iw, head_h + 0.02), item["heading"], hpt, P["colours"]["primary_dark"], P, bold=True)
        y += head_h + 0.1
        if item.get("text"):
            text(slide, Box(col.x, y, iw, text_h + 0.04), item["text"], tpt, P["colours"]["text"], P)
    return problems


def _numeric(v: str) -> bool:
    s = str(v).replace(",", "").replace("%", "").replace("€", "").replace("$", "").replace("£", "").strip()
    try:
        float(s.lstrip("+-~"))
        return True
    except ValueError:
        return False


def table(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A clean table: a brand-coloured header, light banding, numbers right-aligned.

    d: {"header": [...], "rows": [[...]], "highlight_row": <index or null>}
    """
    header, rows = [str(h) for h in d["header"]], [[str(c) for c in r] for r in d["rows"]]
    ncol = len(header)
    weights = [max(len(header[c]), *(len(r[c]) for r in rows), 4) ** 0.8 for c in range(ncol)]
    widths = [box.w * w / sum(weights) for w in weights]
    mx, my = 0.1, 0.06
    numeric = [all(_numeric(r[c]) for r in rows) for c in range(ncol)]

    def heights(pt):
        out = []
        for r, bold in [(header, True)] + [(r, False) for r in rows]:
            lines = max(len(measure.wrap(v, pt, widths[c] - 2 * mx, P["font"], bold)) for c, v in enumerate(r))
            out.append(measure.height_in(lines, pt) + 2 * my + 0.04)
        return out

    pt = P["body_pt"] + 2
    while pt > P["small_pt"] and sum(heights(pt)) > box.h:
        pt -= 0.5
    problems = []
    hs = heights(pt)
    if sum(hs) > box.h:
        problems.append("table: too many rows or too much text; at most ~7 rows of short cells")
    else:
        # Spare height goes into the rows (up to a comfortable 0.6 inch each), so a short
        # table breathes instead of leaving a band of nothing under it.
        extra = min((box.h - sum(hs)) / len(hs), max(0.0, 0.6 - max(hs)))
        hs = [h + extra for h in hs]
    gf = slide.shapes.add_table(len(rows) + 1, ncol, Emu(int(box.x * EMU)), Emu(int(box.y * EMU)),
                                Emu(int(box.w * EMU)), Emu(int(sum(hs) * EMU)))
    tbl = gf.table
    tbl.first_row, tbl.horz_banding = True, False
    for c, w in enumerate(widths):
        tbl.columns[c].width = Emu(int(w * EMU))
    for r, h in enumerate(hs):
        tbl.rows[r].height = Emu(int(h * EMU))
    hl = d.get("highlight_row")
    for r, vals in enumerate([header] + rows):
        for c, v in enumerate(vals):
            cell = tbl.cell(r, c)
            cell.margin_left = cell.margin_right = Emu(int(mx * EMU))
            cell.margin_top = cell.margin_bottom = Emu(int(my * EMU))
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            if r == 0:
                fill, fg, bold = P["colours"]["primary"], "FFFFFF", True
            elif hl is not None and r - 1 == hl:
                fill, fg, bold = P["colours"]["accent_light"], P["colours"]["text"], True
            else:
                band = P["colours"]["primary_light"] if filled(P) else P["colours"]["panel"]
                fill, fg, bold = ("FFFFFF" if r % 2 else band), P["colours"]["text"], c == 0
            cell.fill.fore_color.rgb = _rgb(fill)
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.RIGHT if numeric[c] and r > 0 else PP_ALIGN.LEFT
            run = p.add_run()
            run.text = v
            run.font.size, run.font.bold, run.font.name = Pt(pt), bold, P["font"]
            run.font.color.rgb = _rgb(fg)
    return problems


def big_number(slide, box: Box, d: dict, P: dict) -> list[str]:
    """One number, very large, with what it means. The hero of a slide.

    d: {"value": "73%", "label": "of scans finish under 3 minutes", "text": "...", "tone": "accent"}
    """
    tone = colour(P, d.get("tone"), "accent")
    vpt = measure.fit(d["value"], box.w, box.h * 0.5, 110, 40, family=P["font"], bold=True, max_lines=1)
    problems = []
    if vpt is None:
        problems.append("big_number: the value is too long; a big number is a few characters")
        vpt = 40
    lpt = measure.fit(d.get("label", ""), box.w, box.h * 0.2, P["body_pt"] + 6, P["body_pt"],
                      family=P["font"], bold=True, max_lines=2) or P["body_pt"]
    v_h = measure.height_in(1, vpt) * 0.92
    l_lines = len(measure.wrap(d.get("label", ""), lpt, box.w, P["font"], True)) if d.get("label") else 0
    l_h = measure.height_in(l_lines, lpt)
    tpt = P["body_pt"] + 1
    t_lines = len(measure.wrap(d.get("text", ""), tpt, box.w, P["font"])) if d.get("text") else 0
    t_h = measure.height_in(t_lines, tpt)
    total = v_h + 0.12 + l_h + (0.18 + t_h if t_h else 0)
    if total > box.h:
        problems.append("big_number: too much text under the number; one short sentence at most")
    y = box.y + max(0, (box.h - total) / 2)
    text(slide, Box(box.x, y, box.w, v_h), d["value"], vpt, tone, P, bold=True, anchor="bottom")
    y += v_h + 0.12
    if l_h:
        text(slide, Box(box.x, y, box.w, l_h + 0.02), d["label"], lpt, P["colours"]["primary_dark"], P, bold=True)
        y += l_h + 0.18
    if t_h:
        text(slide, Box(box.x, y, box.w, t_h + 0.02), d["text"], tpt, P["colours"]["muted"], P)
    return problems


def statement(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A single sentence, large, with an accent rule: a claim or a quote worth a slide.

    d: {"text": "...", "by": "optional attribution"}
    """
    w = box.w - 0.5
    by_h = measure.height_in(1, P["body_pt"] + 1) + 0.2 if d.get("by") else 0
    pt = measure.fit(d["text"], w, box.h - by_h, 36, 22, family=P["font"], max_lines=4)
    problems = []
    if pt is None:
        problems.append("statement: too long; a statement is one sentence of ~25 words")
        pt = 22
    lines = len(measure.wrap(d["text"], pt, w, P["font"]))
    h = measure.height_in(lines, pt)
    y = box.y + (box.h - h - by_h) / 2
    rect(slide, Box(box.x, y, 0.1, h + by_h), P["colours"]["accent"])
    text(slide, Box(box.x + 0.5, y, w, h + 0.04), d["text"], pt, P["colours"]["primary_dark"], P)
    if d.get("by"):
        text(slide, Box(box.x + 0.5, y + h + 0.2, w, by_h), d["by"], P["body_pt"] + 1, P["colours"]["muted"], P)
    return problems


def matrix(slide, box: Box, d: dict, P: dict) -> list[str]:
    """A 2x2: two axes, four quadrants, one of them highlighted.

    d: {"x_axis": "Effort", "y_axis": "Impact",
        "quadrants": [{"heading", "text"} x4 in order top-left, top-right, bottom-left, bottom-right],
        "highlight": <0-3 or null>}
    """
    lab_pt = P["body_pt"] - 1
    lab_h = measure.height_in(1, lab_pt) + 0.08
    grid = Box(box.x, box.y + lab_h, box.w, box.h - 2 * lab_h)
    cells = []
    gw, gh = (grid.w - 0.12) / 2, (grid.h - 0.12) / 2
    for r in range(2):
        for c in range(2):
            cells.append(Box(grid.x + c * (gw + 0.12), grid.y + r * (gh + 0.12), gw, gh))
    pad = 0.24
    iw = gw - 2 * pad
    qs = d["quadrants"]
    hpt = measure.fit_all([q["heading"] for q in qs], iw, measure.height_in(1, P["body_pt"] + 3),
                          P["body_pt"] + 3, P["body_pt"], family=P["font"], bold=True, max_lines=1)
    head_h = measure.height_in(1, hpt or P["body_pt"])
    tpt = measure.fit_all([q.get("text", "") for q in qs], iw, gh - 2 * pad - head_h - 0.08,
                          P["body_pt"] + 3, P["small_pt"], family=P["font"])
    problems = []
    if hpt is None:
        problems.append("matrix: a quadrant heading is too long; 1-4 words")
        hpt = P["body_pt"]
    if tpt is None:
        problems.append(_too_long("matrix", [q.get("text", "") for q in qs], iw, gh - 2 * pad - head_h - 0.08, P))
        tpt = P["small_pt"]
    for k, (cell, q) in enumerate(zip(cells, qs)):
        hot = d.get("highlight") == k
        rect(slide, cell, P["colours"]["accent_light"] if hot else P["colours"]["panel"],
             P["colours"]["accent"] if hot else None, 1.5)
        inner = cell.inset(pad)
        text(slide, Box(inner.x, inner.y, iw, head_h + 0.02), q["heading"], hpt,
             P["colours"]["accent_dark"] if hot else P["colours"]["primary_dark"], P, bold=True)
        text(slide, Box(inner.x, inner.y + head_h + 0.08, iw, inner.h - head_h - 0.08), q.get("text", ""),
             tpt, P["colours"]["text"], P)
    text(slide, Box(box.x, box.y, box.w, lab_h), f"↑ {d.get('y_axis', '')}  (low → high, bottom to top)",
         lab_pt, P["colours"]["muted"], P, bold=True)
    text(slide, Box(box.x, grid.bottom + 0.06, box.w, lab_h), f"{d.get('x_axis', '')} →  (low → high, left to right)",
         lab_pt, P["colours"]["muted"], P, bold=True, align="right")
    return problems


PIECES = {"kpis": kpis, "chart": chart, "steps": steps, "callout": callout, "footnote": footnote,
          "points": points, "cards": cards, "compare": compare, "timeline": timeline,
          "table": table, "big_number": big_number, "statement": statement, "matrix": matrix}
