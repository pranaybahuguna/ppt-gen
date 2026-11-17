"""What a slide spec may contain, checked before anything is drawn.

One table, PIECE_RULES, drives three things so they never drift apart:
  * `validate(spec)`  - every problem with a spec, as sentences the model can act on
  * `catalog()`       - the piece list the spec writer is shown in its prompt
  * the minimum grid span each piece needs to look right

Called by: design.plan (validation and the prompt), tests.
"""
from __future__ import annotations

import re

#: Per piece: the list field and how many entries it may have, each field's character limit,
#: the narrowest span (of 12 columns) it works in, and a one-line description for the prompt.
PIECE_RULES: dict[str, dict] = {
    "kpis": {"list": ("items", 2, 4), "fields": {"value": 8, "label": 40}, "required": ["value", "label"],
             "span": 12, "use": "2-4 headline numbers in cards; value is the number itself (e.g. '20/hr'), "
                                "label says what it is. tone per item: primary|primary_dark|accent|alert"},
    "chart": {"list": ("categories", 2, 12), "fields": {"heading": 60, "x_title": 30, "y_title": 30}, "span": 6,
              "use": "a native chart. kind: column|bar|line|stacked|doughnut; categories (labels, <=18 chars); "
                     "series [{name, values}] (1-3 series, numbers only); highlight [indices of bars in the "
                     "accent colour, single series only]; number_format e.g. '0', '0%', '#,##0'; "
                     "optional x_title / y_title (axis titles) and value_axis: true for a scale with "
                     "gridlines - by default the values are labelled on the chart instead"},
    "steps": {"list": ("items", 3, 5), "fields": {"heading": 28, "text": 140}, "required": ["heading", "text"],
              "span": 12, "use": "a numbered left-to-right process; last_is_outcome: true colours the last "
                                 "step as the result"},
    "points": {"list": ("items", 2, 5), "fields": {"heading": 40, "text": 140, "label": 40}, "required": ["text"],
               "span": 4, "use": "a vertical list; each item an optional bold heading and a sentence. "
                                 "label: an optional heading over the whole list; "
                                 "numbered: true for ordered points"},
    "cards": {"list": ("items", 2, 6), "fields": {"value": 6, "heading": 32, "text": 130},
              "required": ["heading"], "span": 8,
              "use": "a grid of cards, each an optional big value, a heading and a short text; "
                     "tone 'accent' on one item marks the recommended one"},
    "compare": {"list": ("columns", 1, 3), "fields": {"label": 22}, "span": 4,
                "use": "1-3 columns under coloured labels (before/after, options); each column "
                       "{label, points: [2-5 sentences of <=90 chars]}. One column is a labelled "
                       "panel of points, e.g. beside a chart (span >= 4 per column)"},
    "timeline": {"list": ("items", 3, 6), "fields": {"when": 12, "heading": 28, "text": 110},
                 "required": ["when", "heading"], "span": 12,
                 "use": "dated milestones on a line; when is short ('Q3 2026', 'Week 2')"},
    "table": {"list": ("rows", 2, 8), "fields": {}, "span": 8,
              "use": "header [2-6 short labels] and rows [[cells of <=40 chars]]; highlight_row: index "
                     "of the row to emphasise"},
    "big_number": {"fields": {"value": 7, "label": 60, "text": 140}, "required": ["value", "label"],
                   "span": 3, "use": "one number shown huge with what it means; the hero of a slide"},
    "statement": {"fields": {"text": 200, "by": 60}, "required": ["text"], "span": 6,
                  "use": "one sentence shown large: a claim or a quote (by = who said it)"},
    "matrix": {"list": ("quadrants", 4, 4), "fields": {"heading": 24, "text": 100, "x_axis": 20, "y_axis": 20},
               "required": ["heading"], "span": 6,
               "use": "a 2x2: x_axis, y_axis, quadrants in order top-left, top-right, bottom-left, "
                      "bottom-right; highlight: 0-3"},
    "callout": {"fields": {"text": 110, "label": 14}, "required": ["text"], "span": 12,
                "use": "a full-width band with the slide's bottom line or decision; always its own last "
                       "row with \"height\": 0.75"},
}

#: Pieces that are short by nature: alone on a slide they leave it half empty.
SHORT = {"kpis", "cards", "compare", "steps", "timeline"}

#: Titles that describe the slide instead of saying something.
META = re.compile(r"^\s*(explain|describe|discuss|introduc|overview|summary|agenda|this slide|"
                  r"we will|let's|here is|here are|an? (overview|summary|look))\b", re.I)


def _str_problems(where: str, obj: dict, limits: dict) -> list[str]:
    out = []
    for f, limit in limits.items():
        v = obj.get(f)
        if v is not None and len(str(v)) > limit:
            out.append(f"{where}: '{f}' is {len(str(v))} characters; at most {limit}")
    return out


def _piece_problems(item: dict, where: str) -> list[str]:
    name = item.get("piece")
    rules = PIECE_RULES.get(name)
    if rules is None:
        return [f"{where}: unknown piece {name!r}; use one of {sorted(PIECE_RULES)}"]
    where = f"{where} ({name})"
    out = []
    span = item.get("span", 12)
    if span < rules["span"]:
        out.append(f"{where}: span {span} is too narrow; it needs at least {rules['span']} of 12")
    # Pieces with a list keep most limits per entry; at the top they have only a heading or axes.
    top = {k: v for k, v in rules["fields"].items()
           if "list" not in rules or k in ("x_axis", "y_axis", "label") or name == "chart"}
    out += _str_problems(where, item, top)
    for f in rules.get("required", []):
        if "list" not in rules and not item.get(f):
            out.append(f"{where}: '{f}' is required")
    if "list" in rules:
        key, lo, hi = rules["list"]
        entries = item.get(key) or []
        if not lo <= len(entries) <= hi:
            out.append(f"{where}: '{key}' has {len(entries)} entries; it takes {lo}-{hi}")
        for i, e in enumerate(entries):
            if isinstance(e, dict):
                out += _str_problems(f"{where} {key}[{i}]", e,
                                     {k: v for k, v in rules["fields"].items() if k not in ("x_axis", "y_axis")})
                out += [f"{where} {key}[{i}]: '{f}' is required" for f in rules.get("required", [])
                        if not e.get(f)]
    if name == "chart":
        n = len(item.get("categories") or [])
        series = item.get("series") or []
        if not 1 <= len(series) <= 3:
            out.append(f"{where}: 1-3 series, not {len(series)}")
        for s in series:
            vals = s.get("values") or []
            if len(vals) != n:
                out.append(f"{where}: series {s.get('name')!r} has {len(vals)} values for {n} categories")
            if any(not isinstance(v, (int, float)) for v in vals):
                out.append(f"{where}: series {s.get('name')!r} values must be plain numbers")
        if item.get("kind", "column") not in ("column", "bar", "line", "stacked", "doughnut"):
            out.append(f"{where}: kind must be column, bar, line, stacked or doughnut")
        out += [f"{where}: category {c!r} is longer than 18 characters" for c in item.get("categories") or []
                if len(str(c)) > 18]
    if name == "compare":
        cols = len(item.get("columns") or [])
        if item.get("span", 12) < 4 * cols:
            out.append(f"{where}: {cols} columns need a span of at least {4 * cols}")
        for i, c in enumerate(item.get("columns") or []):
            pts = c.get("points") or []
            if not 2 <= len(pts) <= 5:
                out.append(f"{where} columns[{i}]: 2-5 points, not {len(pts)}")
            out += [f"{where} columns[{i}]: a point is {len(p)} characters; at most 90" for p in pts if len(p) > 90]
    if name == "table":
        header = item.get("header") or []
        if not 2 <= len(header) <= 6:
            out.append(f"{where}: header has {len(header)} columns; 2-6")
        for r in item.get("rows") or []:
            if len(r) != len(header):
                out.append(f"{where}: a row has {len(r)} cells for {len(header)} columns")
            out += [f"{where}: a cell is {len(str(c))} characters; at most 40" for c in r if len(str(c)) > 40]
    return out


def validate(spec: dict) -> list[str]:
    """Every problem with one slide spec, as sentences a model can act on. [] when it is fine."""
    kind = spec.get("type", "content")
    title = str(spec.get("title") or "")
    out: list[str] = []
    if not title:
        out.append("the slide needs a title")
    if kind == "cover":
        out += _str_problems("cover", spec, {"title": 90, "subtitle": 120, "meta": 60})
        out += [f"cover agenda: '{a}' is longer than 40 characters" for a in spec.get("agenda") or [] if len(a) > 40]
        return out
    if kind == "section":
        return out + _str_problems("section", spec, {"title": 60, "subtitle": 100})
    if META.search(title):
        out.append(f"title {title!r} describes the slide; state its point as a sentence instead")
    out += _str_problems("slide", spec, {"title": 90, "subtitle": 110, "footnote": 200})
    rows = spec.get("rows") or []
    if not 1 <= len(rows) <= 3:
        out.append(f"the slide has {len(rows)} rows; use 1-3")
    for r_i, row in enumerate(rows):
        items = row.get("items") or []
        if not items:
            out.append(f"row {r_i + 1} is empty")
        spans = sum(i.get("span", 12 // max(1, len(items))) for i in items)
        if spans > 12:
            out.append(f"row {r_i + 1}: spans add up to {spans}; at most 12")
        for i_i, item in enumerate(items):
            out += _piece_problems(item, f"row {r_i + 1} item {i_i + 1}")
            if item.get("piece") == "callout" and (r_i != len(rows) - 1 or len(items) != 1 or "height" not in row):
                out.append("a callout is the last row on its own, with \"height\": 0.75")
    for r in rows:
        for it in r.get("items") or []:
            if it.get("piece") == "big_number" and not re.search(r"\d", str(it.get("value", ""))):
                out.append(f"big_number value {it.get('value')!r} is not a number; use a figure from "
                           "the facts, or a different piece")
    words = _words(rows)
    if words > MAX_WORDS:
        out.append(f"the slide carries {words} words; at most {MAX_WORDS}. Cut to the point: fewer "
                   "items, shorter texts, or move detail to the speaker notes")
    pieces = [i.get("piece") for r in rows for i in r.get("items") or []]
    if len(rows) == 1 and len(pieces) == 1 and pieces[0] in SHORT:
        out.append(f"a {pieces[0]} alone leaves the slide half empty; add a callout row with the "
                   "bottom line, or a second row (a chart, points)")
    return out


#: More words than this and the text has to shrink below a comfortable reading size.
MAX_WORDS = 110


def _words(obj) -> int:
    """Words a slide shows in its pieces (not its title, footnote or notes)."""
    if isinstance(obj, dict):
        return sum(_words(v) for k, v in obj.items() if k not in ("piece", "kind", "tone", "number_format"))
    if isinstance(obj, list):
        return sum(_words(v) for v in obj)
    return len(obj.split()) if isinstance(obj, str) else 0


def catalog() -> str:
    """The piece list for the spec writer's prompt, from PIECE_RULES."""
    lines = []
    for name, r in PIECE_RULES.items():
        limits = ", ".join(f"{k}<={v}" for k, v in r["fields"].items())
        lst = f"; {r['list'][0]}: {r['list'][1]}-{r['list'][2]} entries" if "list" in r else ""
        lines.append(f"  {name} (span >= {r['span']}): {r['use']}{lst}" + (f"; chars: {limits}" if limits else ""))
    return "\n".join(lines)


_NUM = re.compile(r"\d[\d,.]*")


def numbers_in(obj) -> set[str]:
    """Every number written in a spec's text (and chart values), normalised: '1,200' -> '1200'."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in ("span", "weight", "height", "highlight", "highlight_row", "number", "columns"):
                found |= numbers_in(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= numbers_in(v)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        found.add(_plain(str(obj)))
    elif isinstance(obj, str):
        found |= {_plain(m.group()) for m in _NUM.finditer(obj)}
    return found


def _plain(n: str) -> str:
    n = n.replace(",", "").rstrip(".")
    return n[:-2] if n.endswith(".0") else n


def unsourced(spec: dict, source_text: str) -> list[str]:
    """Numbers on the slide that appear nowhere in the facts: invented, or miscopied."""
    have = {_plain(m.group()) for m in _NUM.finditer(source_text)}
    small = {str(i) for i in range(0, 11)}            # counts and list numbers are not claims
    return sorted(n for n in numbers_in(spec) if n not in have and n not in small)
