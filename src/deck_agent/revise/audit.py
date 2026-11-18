"""The deck check: the whole deck looked over once more, after every slide has passed review.

Review looks at each changed slide on its own. Some problems only show across slides, or only in
a chart's numbers: the same category in two colours on two slides, two series one can barely
tell apart, a chart that prints 8 as "800%", titles at three different sizes, a figure that
disagrees with the source. This module finds them.

Two kinds of answer:

* fixes done here by plain code, the same way every time (`fix_deck`): percent formats on
  values that are already percentages, chart colours (distinct within a chart, one colour per
  category across the deck, on the deck's palette), one title size on content slides;
* findings for a person or a model to act on (`text_findings`, and the model check fed by
  `inventory`): text too small to read, wrong units, mismatched axis titles, claims that
  contradict the facts. revise.nodes.audit sends these back through `work` as tweaks while there
  are revision rounds left, and reports the rest as warnings.

Called by: revise.nodes.audit.
"""
from __future__ import annotations

import re
from collections import Counter

from pptx.oxml.ns import qn
from pptx.util import Pt

from ..design import measure
from ..design.profile import _hsl, with_palette
from .read import _chart, _leaves

EMU = 914400
SMALL_PT = 10.0           # text under this is hard to read when presented - unless the template
                          # itself uses smaller text (its labels, its footnotes): then its size is
                          # the floor, and only text smaller than anything the template uses is found


# --------------------------------------------------------------------------- charts

def charts(prs, slides: list[int]):
    """(slide number, chart shape) for every chart on `slides`, groups opened."""
    for n in slides:
        if 1 <= n <= len(prs.slides):
            for sh, _ in _leaves(prs.slides[n - 1].shapes):
                if sh.has_chart:
                    yield n, sh


def _values(ch) -> list[float]:
    out = []
    for plot in ch.plots:
        for s in plot.series:
            out += [v for v in s.values if isinstance(v, (int, float))]
    return out


def fix_percent_formats(prs, slides: list[int]) -> list[str]:
    """A chart whose numbers are already percentages (8, 21, 62) but whose format is "0%" shows
    800%, 2100%, 6200%. Make the % sign literal, so the number shows as it is. -> what changed
    """
    done = []
    for n, sh in charts(prs, slides):
        vals = _values(sh.chart)
        if not vals or max(abs(v) for v in vals) <= 1.5:
            continue                       # 0.08 shown as 8%: the format is right
        hit = False
        for nf in sh.chart._chartSpace.iter(qn("c:numFmt")):
            code = nf.get("formatCode") or ""
            if "%" in code.replace('"%"', "").replace("\\%", ""):
                nf.set("formatCode", re.sub(r'(?<!")%(?!")', '"%"', code))
                nf.set("sourceLinked", "0")
                hit = True
        if hit:
            done.append(f"slide {n}: chart numbers were multiplied by 100 (8 shown as 800%); now shown as they are")
    return done


def _dist(a: str, b: str) -> float:
    """How different two RRGGBB colours look ("redmean" weighted RGB distance, 0-765)."""
    r1, g1, b1 = (int(a[i:i + 2], 16) for i in (0, 2, 4))
    r2, g2, b2 = (int(b[i:i + 2], 16) for i in (0, 2, 4))
    rm = (r1 + r2) / 2
    return ((2 + rm / 256) * (r1 - r2) ** 2 + 4 * (g1 - g2) ** 2 + (2 + (255 - rm) / 256) * (b1 - b2) ** 2) ** 0.5


TOO_CLOSE = 90            # two chart colours closer than this are hard to tell apart

_ORDER = ("primary", "accent", "teal", "blue", "amber", "plum", "primary_dark", "accent_dark", "alert")


def chart_palette(P: dict, *more: dict) -> list[str]:
    """The colours a chart may use, in the order to hand them out: the brand's, then the
    extended hues (which are made from the brand's own depth, so they still belong to it).

    `more` are other readings of the same brand: the slides were drawn with the template's
    colours, while counting colours on the finished deck can name another one (its navy section
    panels outnumber everything) - a colour either reading calls the brand's is the brand's."""
    out = []
    for prof in (P, *more):
        c = with_palette(prof, "extended")["colours"]
        for k in _ORDER:
            if c.get(k) and c[k].upper() not in out:
                out.append(c[k].upper())
    return out


def _neutral(hex6: str) -> bool:
    _, l, s = _hsl(hex6)
    return s < 0.18 or l < 0.08 or l > 0.92


def _stem(name: str) -> str:
    """A category's name, so "Electric", "electricity" and "EVs" style variants meet."""
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    out = []
    for w in words:
        for suf in ("icity", "ity", "ic", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)]
                break
        out.append(w)
    return " ".join(out)


def _texts(el, tag: str) -> list[str]:
    """The cached values under a series' c:tx or c:cat, in point order."""
    node = el.find(qn(tag)) if el is not None else None
    if node is None:
        return []
    pts = sorted(node.iter(qn("c:pt")), key=lambda p: int(p.get("idx") or 0))
    if pts:
        return ["".join(v.text or "" for v in p.iter(qn("c:v"))) for p in pts]
    return ["".join(v.text or "" for v in node.iter(qn("c:v")))]


def _paint(el) -> dict | None:
    """The colour a series or a point is drawn in, and every colour element to change with it.

    What shows is the element's own c:spPr (a bar's or slice's fill, a line's line); a c:marker
    colour only shows on a line chart's points, and code often leaves a stale one on a bar chart,
    so it never decides the colour, but follows it when it is a real colour."""
    sp = el.find(qn("c:spPr"))
    own = list(sp.iter(qn("a:srgbClr"))) if sp is not None else []
    marker = el.find(qn("c:marker"))
    mk = [e for e in marker.iter(qn("a:srgbClr")) if not _neutral(e.get("val") or "000000")] \
        if marker is not None else []
    if not own and not mk:
        return None
    colour = (own or mk)[0].get("val").upper()
    return {"colour": colour, "els": [e for e in own if (e.get("val") or "").upper() == colour] + mk}


def _holders(sh) -> list[dict]:
    """Everything coloured in one chart that stands for a named thing: each series, or each point
    of a series whose points are coloured one by one (a pie, a bar per category). Read from the
    chart's XML, so combination charts and charts made elsewhere read the same way."""
    out = []
    for k, ser in enumerate(sh.chart._chartSpace.iter(qn("c:ser"))):
        name = (_texts(ser, "c:tx") or [""])[0] or f"series {k + 1}"
        cats = _texts(ser, "c:cat")
        dpts = ser.findall(qn("c:dPt"))
        for d in dpts:
            idx = d.find(qn("c:idx"))
            i = int(idx.get("val")) if idx is not None else -1
            paint = _paint(d)
            if paint and 0 <= i < len(cats):
                out.append({"name": cats[i], **paint})
        if cats and len(dpts) >= len(cats):
            continue                     # every point has its own colour: the series' is unseen
        paint = _paint(ser)
        if paint:
            out.append({"name": name, **paint, "series": True})
    return out


def fix_chart_colours(prs, slides: list[int], P: dict, *more: dict) -> list[str]:
    """Chart colours, decided once for the whole deck:

    * the same thing (by name) is the same colour on every slide;
    * things shown together in one chart are clearly different colours;
    * colours come from the deck's palette (brand plus its extended hues); greys stay grey.

    Each name keeps the colour it has most often when that is allowed; otherwise it gets the
    nearest palette colour no neighbour of it is using. `more`: other profiles of the same brand
    (see chart_palette). -> what changed
    """
    palette = chart_palette(P, *more)
    found = []                                         # (slide, [holders]) per chart
    for n, sh in charts(prs, slides):
        hs = [h for h in _holders(sh) if not _neutral(h["colour"])]
        if hs:
            found.append((n, hs))
    if not found:
        return []
    seen: dict[str, Counter] = {}
    neighbours: dict[str, set] = {}
    for _, hs in found:
        keys = [_stem(h["name"]) for h in hs]
        for h, k in zip(hs, keys):
            seen.setdefault(k, Counter())[h["colour"]] += 1
            neighbours.setdefault(k, set()).update(x for x in keys if x != k)
    order = sorted(seen, key=lambda k: (-sum(seen[k].values()), k))
    chosen: dict[str, str] = {}
    for k in order:
        taken = [chosen[x] for x in neighbours[k] if x in chosen]

        def free(c: str) -> bool:
            return all(_dist(c, t) >= TOO_CLOSE for t in taken)

        own = [c for c, _ in seen[k].most_common()]
        pick = next((c for c in own if c in palette and free(c)), None)
        if pick is None:
            near = sorted(palette, key=lambda p: min(_dist(p, c) for c in own))
            pick = next((c for c in near if free(c)), near[0])
        chosen[k] = pick
    done = []
    for n, hs in found:
        moved = []
        for h in hs:
            new = chosen[_stem(h["name"])]
            if new != h["colour"]:
                for e in h["els"]:
                    e.set("val", new)
                moved.append(f"{h['name']} {h['colour']}->{new}")
        if moved:
            done.append(f"slide {n}: chart colours " + ", ".join(moved))
    return done


# --------------------------------------------------------------------------- titles

def _title_pt(slide) -> float | None:
    t = slide.shapes.title
    if t is None or not t.text_frame.text.strip():
        return None
    sizes = [r.font.size.pt for p in t.text_frame.paragraphs for r in p.runs if r.font.size]
    return max(sizes) if sizes else None


def fix_title_sizes(prs, slides: list[int], P: dict) -> list[str]:
    """Every content slide's title at the size most of them use, when it fits in two lines.
    A title too long for that size keeps its own and is reported. -> what changed"""
    sizes = {n: _title_pt(prs.slides[n - 1]) for n in slides if 1 <= n <= len(prs.slides)}
    sizes = {n: s for n, s in sizes.items() if s}
    if len(set(sizes.values())) <= 1:
        return []
    common = Counter(sizes.values()).most_common(1)[0][0]
    t = P["title"]
    done = []
    for n, pt in sizes.items():
        if pt == common:
            continue
        title = prs.slides[n - 1].shapes.title
        w = (title.width or 0) / EMU or t["box"][2]
        # Two lines is the rule (as when slides are written); the box's height is not the limit.
        if not measure.fits(title.text_frame.text.strip(), common, w, 10, family=t["font"],
                            bold=True, max_lines=2):
            done.append(f"slide {n}: title left at {pt:g}pt - too long for {common:g}pt in two lines")
            continue
        for p in title.text_frame.paragraphs:
            for r in p.runs:
                r.font.size = Pt(common)
        done.append(f"slide {n}: title {pt:g}pt -> {common:g}pt, like the other content slides")
    return done


# --------------------------------------------------------------------------- findings

def text_findings(prs, slides: list[int], P: dict | None = None) -> dict[int, list[str]]:
    """Text too small to read when presented, per slide. Footers and page numbers do not count;
    neither does a footnote (italic, or along the bottom of the slide)."""
    H = prs.slide_height / EMU
    floor = min(SMALL_PT, (P or {}).get("small_pt") or SMALL_PT)
    out: dict[int, list[str]] = {}
    for n in slides:
        if not 1 <= n <= len(prs.slides):
            continue
        small = []
        for sh, _ in _leaves(prs.slides[n - 1].shapes):
            if sh.is_placeholder and sh.placeholder_format.type is not None and \
                    str(sh.placeholder_format.type).split(".")[-1].split(" ")[0] in ("SLIDE_NUMBER", "FOOTER", "DATE"):
                continue
            if sh.has_chart:
                for el in sh.chart._chartSpace.iter(qn("a:defRPr")):
                    sz = el.get("sz")
                    if sz and int(sz) / 100 < floor:
                        small.append(f"chart text at {int(sz) / 100:g}pt")
                        break
                continue
            if not sh.has_text_frame or not sh.text_frame.text.strip():
                continue
            if ((sh.top or 0) / EMU) > H * 0.86:
                continue
            for p in sh.text_frame.paragraphs:
                runs = [r for r in p.runs if r.text.strip() and r.font.size]
                if runs and all(r.font.italic for r in runs):
                    continue
                tiny = [r for r in runs if r.font.size.pt < floor]
                if tiny:
                    small.append(f"'{p.text.strip()[:40]}' at {tiny[0].font.size.pt:g}pt")
                    break
        if small:
            out[n] = [f"text too small to read when presented ({'; '.join(small[:3])}): make it at "
                      f"least {floor:g}pt and give it the room it needs"]
    return out


def _axis_titles(ch) -> dict:
    out = {}
    for tag, name in (("c:valAx", "value_axis_title"), ("c:catAx", "category_axis_title")):
        for ax in ch._chartSpace.iter(qn(tag)):
            t = ax.find(qn("c:title"))
            if t is not None:
                text = "".join(x.text or "" for x in t.iter(qn("a:t"))).strip()
                if text:
                    out[name] = text
    return out


def inventory(prs, roles: dict[int, str]) -> list[dict]:
    """What every slide says and shows, as text, for the model check: titles, texts, tables, and
    each chart's heading, series, values, units, axis titles and number formats."""
    from .read import slide_content

    out = []
    for n, slide in enumerate(prs.slides, start=1):
        c = slide_content(slide)
        entry = {"slide": n, "role": roles.get(n), "title": c["title"],
                 "texts": [t["text"][:400] for t in c["texts"]][:20]}
        if c["tables"]:
            entry["tables"] = [rows[:12] for rows in c["tables"]]
        cs = []
        for sh, _ in _leaves(slide.shapes):
            if not sh.has_chart:
                continue
            data = _chart(sh)
            fmts = sorted({nf.get("formatCode") for nf in sh.chart._chartSpace.iter(qn("c:numFmt"))
                           if nf.get("formatCode")})
            colours = [h["name"] + " " + h["colour"] for h in _holders(sh)]
            cs.append({**data, **_axis_titles(sh.chart), "number_formats": fmts, "colours": colours})
        if cs:
            entry["charts"] = cs
        out.append(entry)
    return out


def fix_deck(prs, P: dict, content: list[int], every: list[int], *more: dict) -> list[str]:
    """All the plain-code fixes, in place. `content` are the content slides (titles), `every` all
    slides (charts may sit on any); `more` other profiles of the same brand (the template's).
    -> one line per change"""
    return (fix_percent_formats(prs, every) + fix_chart_colours(prs, every, P, *more)
            + fix_title_sizes(prs, content, P))
