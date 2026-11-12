"""Create a new deck in the look of an existing one: "copy an example slide, swap the content".

Why this exists: many real corporate decks have no usable placeholders. Every title and box
is a hand-placed shape with fixed colours, all on one blank layout. Their design lives in the
example slides themselves, not in the template's layouts. So instead of "fill the slots of a
layout", we copy a good example slide and change only its words (and, for repeated items such
as cards or columns, how many there are and where they sit).

A STYLE KIT (JSON, written once per template, by a person) says which example slide is good for
what, and which shape on it holds which piece of content:

    {
      "name": "Sample Co",
      "page_format": "{n} / {total}",
      "patterns": {
        "columns": {
          "slide": 2,                                   # example slide (1-based)
          "use_for": "2-4 parallel themes, each with a heading and bullet points",
          "fields": {"title": 3, "subtitle": 4, "footer": 6, "page": 19},   # name -> shape id
          "items": {                                    # optional repeated block
            "groups": [[7, 8, 9, 10], [11, 12, 13, 14], [15, 16, 17, 18]], # shape ids per item
            "fields": {"heading": 1, "points": 3},      # name -> position inside a group
            "min": 2, "max": 4, "max_columns": 4
          }
        },
        "table": {"slide": 24, "fields": {...}, "table": {"shape": 25, "max_rows": 9}}
      }
    }

The MODEL fills a plan that names patterns and supplies text only:

    {"footer": "...", "slides": [
       {"pattern": "columns", "fields": {"title": "...", "subtitle": "..."},
        "items": [{"heading": "Today", "points": ["...", "..."]}, ...]}]}

compile_from_kit (pure, no model) turns that into ops: copy slide N, duplicate/delete item
shapes to match the item count, re-flow the items in a grid, write the text, number the pages,
then delete every original example slide.

Safety rule: any text shape on the copied slide that the plan does not fill is DELETED, so no
example content can leak into the new deck.
"""
from __future__ import annotations

import json
import math

from .compiler import CompileError

AUTO_FIELDS = {"page", "footer"}     # filled by the compiler, not the model
CHART_TYPES = ["bar", "column", "stacked_column", "line", "pie", "doughnut"]


def compile_chart(spec: dict, datasets: dict, style: dict, where: str) -> tuple[dict | None, list[str]]:
    """The model picks WHICH data and WHAT chart; the numbers are copied from the dataset here.

    spec    = {"type": "column", "dataset": "monthly_users", "category": "Month",
               "series": ["Users"], "title": "...", "number_format": "#,##0", "show_values": true}
    dataset = {"columns": ["Month", "Users"], "rows": [["Jul", 1200], ...], "source": "..."}
    """
    errs = []
    if spec.get("type") not in CHART_TYPES:
        return None, [f"{where}: chart type must be one of {CHART_TYPES}"]
    ds = datasets.get(spec.get("dataset"))
    if ds is None:
        return None, [f"{where}: unknown dataset {spec.get('dataset')!r}; available: {sorted(datasets)}"]
    cols = ds["columns"]
    cat, series = spec.get("category"), spec.get("series") or []
    missing = [c for c in [cat, *series] if c not in cols]
    if missing or not series:
        return None, [f"{where}: dataset {spec['dataset']!r} has columns {cols}; missing {missing or 'series'}"]
    ci = cols.index(cat)
    out_series = []
    for name in series:
        si = cols.index(name)
        vals = [r[si] for r in ds["rows"]]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            errs.append(f"{where}: column {name!r} is not all numbers")
        out_series.append({"name": name, "values": vals})
    if spec["type"] in ("pie", "doughnut"):
        if len(series) != 1:
            errs.append(f"{where}: a {spec['type']} chart shows exactly one series")
        elif any(v < 0 for v in out_series[0]["values"] if isinstance(v, (int, float))):
            errs.append(f"{where}: a {spec['type']} chart can't show negative values")
        if len(ds["rows"]) > 8:
            errs.append(f"{where}: a {spec['type']} chart is unreadable with more than 8 slices")
    if len(ds["rows"]) > 24:
        errs.append(f"{where}: too many categories for one chart (max 24)")
    if errs:
        return None, errs
    return {"op": "set_chart", "chart_type": spec["type"], "categories": [str(r[ci]) for r in ds["rows"]],
            "series": out_series, "title": spec.get("title", ""), "number_format": spec.get("number_format", "General"),
            "show_values": spec.get("show_values", True), "palette": style.get("palette", ["4F46E5"]),
            "font": style.get("font", "Calibri"), "text_colour": style.get("text_colour", "374151"),
            "grid_colour": style.get("grid_colour", "E5E7EB")}, []


def load_kit(path: str) -> dict:
    """Read a kit file and check every pattern names its example slide and fields.

    Called by: the engine's standalone paths (orchestrator, deckops_cli) and its tests; the agent
    reads kits from object storage instead.
    """
    kit = json.load(open(path))
    for name, p in kit["patterns"].items():
        if "slide" not in p or "fields" not in p:
            raise CompileError([f"pattern {name!r}: needs 'slide' and 'fields'"])
    return kit


def validate_kit(kit: dict, index: dict) -> list[str]:
    """Check every id in the kit against the probed template. Returns a list of problems."""
    errs = []
    n_slides = len(index["slides"])
    for name, p in kit["patterns"].items():
        where = f"kit pattern {name!r}"
        if not isinstance(p.get("slide"), int) or not 1 <= p["slide"] <= n_slides:
            errs.append(f"{where}: slide {p.get('slide')} not in template (1-{n_slides})")
            continue
        shapes = _shapes_by_id(index["slides"][p["slide"] - 1])
        seen = []
        def need(sid, what, text=False):
            """Record an error if shape `sid` is missing from the example slide (or cannot hold text).
            """
            seen.append(sid)
            if sid not in shapes:
                errs.append(f"{where}: {what}: slide {p['slide']} has no shape {sid}")
            elif text and "paras" not in shapes[sid] and "cells" not in shapes[sid]:
                errs.append(f"{where}: {what}: shape {sid} can't hold text")
        for fname, sid in p["fields"].items():
            need(sid, f"field {fname!r}", text=True)
        for slot, sid in p.get("charts", {}).items():
            need(sid, f"chart slot {slot!r}")
        if "table" in p:
            need(p["table"]["shape"], "table")
            if p["table"]["shape"] in shapes and "cells" not in shapes[p["table"]["shape"]]:
                errs.append(f"{where}: shape {p['table']['shape']} is not a table")
        if "items" in p:
            groups = p["items"]["groups"]
            if not groups or len({len(g) for g in groups}) != 1:
                errs.append(f"{where}: item groups must all have the same number of shapes")
            for gi, g in enumerate(groups):
                for sid in g:
                    need(sid, f"item {gi + 1}")
            for fname, pos in p["items"]["fields"].items():
                if groups and not 0 <= pos < len(groups[0]):
                    errs.append(f"{where}: item field {fname!r} points at position {pos}, groups have {len(groups[0])}")
                elif groups:
                    for g in groups:
                        if g[pos] in shapes and "paras" not in shapes[g[pos]]:
                            errs.append(f"{where}: item field {fname!r}: shape {g[pos]} can't hold text")
        for sid in p.get("keep", []):
            if sid not in shapes:
                errs.append(f"{where}: keep: slide {p['slide']} has no shape {sid}")
        dup = {s for s in seen if seen.count(s) > 1}
        if dup:
            errs.append(f"{where}: shapes used twice: {sorted(dup)}")
    return errs


def describe_kit(kit: dict) -> str:
    """What the model is told about the available slide patterns (no geometry, no colours)."""
    lines = []
    for name, p in kit["patterns"].items():
        fields = [f for f in p["fields"] if f not in AUTO_FIELDS]
        s = f"- {name}: {p.get('use_for', '')}. fields: {fields}"
        if "items" in p:
            it = p["items"]
            s += f"; items: {it.get('min', 1)}-{it.get('max', 12)} of {list(it['fields'])}"
        if "table" in p:
            s += f"; table: header + up to {p['table'].get('max_rows', 10)} rows"
        if "charts" in p:
            s += f"; charts: {list(p['charts'])} (each: type, dataset, category column, series columns)"
        lines.append(s)
    lines.append("Chart types: " + ", ".join(CHART_TYPES) + ". Charts take their numbers from a content-pack "
                 "dataset by id; you choose the dataset, the category column and the value columns.")
    return "\n".join(lines)


# ---- helpers over the DeckIndex ----------------------------------------------------------

def _shapes_by_id(slide: dict) -> dict[int, dict]:
    """A probed slide's shapes keyed by shape id."""
    return {int(s["ref"].rsplit("shape:", 1)[1]): s for s in slide["shapes"]}


def _text_of(shape: dict) -> str:
    """All the text of a probed shape, table cells included."""
    t = "".join(p["text"] for p in shape.get("paras", []))
    t += "".join(p["text"] for c in shape.get("cells", []) for p in c["paras"])
    return t.strip()


def _as_paragraphs(value) -> list[str]:
    """A field value as a list of paragraphs: a list as given, a string split on newlines."""
    if isinstance(value, list):
        return [str(v) for v in value] or [""]
    return str(value).split("\n")


def _bbox(geoms):
    """The box around a set of geometries, as (x, y, w, h)."""
    x0 = min(g["x"] for g in geoms); y0 = min(g["y"] for g in geoms)
    x1 = max(g["x"] + g["w"] for g in geoms); y1 = max(g["y"] + g["h"] for g in geoms)
    return x0, y0, x1 - x0, y1 - y0


def _stretch(offset, size, margin, group_size, new_group_size):
    """Shapes that reach (nearly) to the group's far edge grow with the group; small ones keep
    size.
    """
    if size > 0.3 * group_size and margin < 0.25 * group_size:
        return max(1, int(new_group_size - offset - margin))
    return size


def layout_items(groups_geom: list[list[dict]], n: int, max_columns: int | None = None):
    """Grid for n items inside the area the example's items occupied.

    groups_geom: per example group, the geometry of each of its shapes.
    Returns, per new item i: (template_group_index, [new geom per shape]).
    """
    gboxes = [_bbox(g) for g in groups_geom]
    span = _bbox([{"x": b[0], "y": b[1], "w": b[2], "h": b[3]} for b in gboxes])
    xs = sorted({b[0] for b in gboxes}); ys = sorted({b[1] for b in gboxes})
    w0, h0 = gboxes[0][2], gboxes[0][3]
    gap_x = (xs[1] - xs[0] - w0) if len(xs) > 1 else 228600          # default 0.25in
    gap_y = (ys[1] - ys[0] - h0) if len(ys) > 1 else 182880
    # default: a single-row example stays a single row; a grid keeps its column count
    max_cols = max_columns or (len(xs) if len(ys) > 1 else n)
    rows = math.ceil(n / max_cols)
    cols = math.ceil(n / rows)
    item_w = (span[2] - gap_x * (cols - 1)) / cols
    item_h = h0 if rows == len(ys) else (span[3] - gap_y * (rows - 1)) / rows
    out = []
    for i in range(n):
        k = i % len(groups_geom)                                   # cycle example groups (keeps colour order)
        gx, gy, gw, gh = gboxes[k]
        ix = span[0] + (i % cols) * (item_w + gap_x)
        iy = span[1] + (i // cols) * (item_h + gap_y)
        new = []
        for g in groups_geom[k]:
            dx, dy = g["x"] - gx, g["y"] - gy
            w = _stretch(dx, g["w"], gx + gw - (g["x"] + g["w"]), gw, item_w)
            h = _stretch(dy, g["h"], gy + gh - (g["y"] + g["h"]), gh, item_h)
            new.append({"x": int(ix + dx), "y": int(iy + dy), "w": int(w), "h": int(h)})
        out.append((k, new))
    return out


def _spill(groups_geom, placed, tol=45720):
    """Do the laid-out items leave the area the example's items occupied? (0.05in tolerance)"""
    if not placed:
        return None
    x0, y0, w, h = _bbox([{"x": b[0], "y": b[1], "w": b[2], "h": b[3]} for b in map(_bbox, groups_geom)])
    bottom = max(g["y"] + g["h"] for _, gs in placed for g in gs)
    right = max(g["x"] + g["w"] for _, gs in placed for g in gs)
    if bottom > y0 + h + tol:
        return f"they would run {(bottom - y0 - h) / 914400:.2f} in below their area"
    if right > x0 + w + tol:
        return f"they would run {(right - x0 - w) / 914400:.2f} in past their area"
    return None


# ---- the compiler ---------------------------------------------------------------------------

def compile_from_kit(plan: dict, index: dict, kit: dict, content: dict | None = None) -> list[dict]:
    """Compile a plan against a kit into operations: copy each pattern's example slide, fill its
    fields, lay out its items (adding or removing repeats to match the count), fill charts and
    tables, and delete what is left over. Pure - no file is touched. Raises CompileError with
    messages written to be fed back to a model.

    Called by: the agent's engine.build.compile_plan (every build, rebuild and kit test).
    """
    kit_errors = validate_kit(kit, index)
    if kit_errors:
        raise CompileError(kit_errors)
    errors, ops = [], []
    datasets = (content or {}).get("datasets", {})
    total = len(plan["slides"])
    for n, s in enumerate(plan["slides"], start=1):
        where = f"slide {n} ({s.get('pattern')})"
        pat = kit["patterns"].get(s.get("pattern"))
        if pat is None:
            errors.append(f"{where}: unknown pattern; available: {sorted(kit['patterns'])}")
            continue
        ex = index["slides"][pat["slide"] - 1]
        shapes = _shapes_by_id(ex)
        steps, used = [], set()
        given = dict(s.get("fields", {}))
        unknown = set(given) - set(pat["fields"])
        if unknown:
            errors.append(f"{where}: unknown fields {sorted(unknown)}")
        auto = {"page": kit.get("page_format", "{n} / {total}").format(n=n, total=total),
                "footer": plan.get("footer")}
        for name, sid in pat["fields"].items():
            used.add(sid)
            value = auto.get(name) if name in AUTO_FIELDS else given.get(name)
            if value in (None, "", []):
                steps.append({"op": "delete_shape", "target": f"shape:{sid}"})
            else:
                steps.append({"op": "set_shape_text", "target": f"shape:{sid}", "paragraphs": _as_paragraphs(value)})

        # repeated items (cards, columns, stat tiles, panels)
        if "items" in pat:
            it = pat["items"]
            items = s.get("items", [])
            lo, hi = it.get("min", 1), it.get("max", 12)
            if not lo <= len(items) <= hi:
                errors.append(f"{where}: needs {lo}-{hi} items, got {len(items)}")
                items = items[:hi] or [{}]
            groups = it["groups"]
            used.update(sid for g in groups for sid in g)
            geoms = [[shapes[sid]["geom"] for sid in g] for g in groups]
            next_id = max(shapes) + 1
            dup_steps, del_steps, geo_steps, text_steps = [], [], [], []
            placed = layout_items(geoms, len(items), it.get("max_columns")) if items else []
            spill = _spill(geoms, placed)
            if spill:
                errors.append(f"{where}: {len(items)} items don't fit this layout ({spill}); "
                              f"use fewer items or split them over two slides")
            for i, (k, new_geoms) in enumerate(placed):
                if i < len(groups):
                    ids = groups[i]
                else:                                            # more items than the example has
                    ids = []
                    for sid in groups[k]:
                        dup_steps.append({"op": "duplicate_shape", "source": f"shape:{sid}", "new_id": next_id})
                        ids.append(next_id)
                        next_id += 1
                for sid, g in zip(ids, new_geoms):
                    geo_steps.append({"op": "set_geometry", "target": f"shape:{sid}", **g})
                item = dict(items[i])
                bad = set(item) - set(it["fields"])
                if bad:
                    errors.append(f"{where} item {i + 1}: unknown fields {sorted(bad)}")
                if "number" in it["fields"] and "number" not in item:
                    item["number"] = str(i + 1)
                for fname, pos in it["fields"].items():
                    sid = ids[pos]
                    v = item.get(fname)
                    if v in (None, "", []):
                        del_steps.append({"op": "delete_shape", "target": f"shape:{sid}"})
                    else:
                        text_steps.append({"op": "set_shape_text", "target": f"shape:{sid}",
                                           "paragraphs": _as_paragraphs(v)})
            for g in groups[len(items):]:                        # fewer items than the example has
                del_steps += [{"op": "delete_shape", "target": f"shape:{sid}"} for sid in g]
            steps = dup_steps + steps + geo_steps + text_steps + del_steps

        if "table" in pat:
            t, tsid = s.get("table"), pat["table"]["shape"]
            used.add(tsid)
            ncols = 1 + max(c["rc"][1] for c in shapes[tsid]["cells"])
            if not t:
                errors.append(f"{where}: needs a table")
            elif len(t["header"]) != ncols or any(len(r) != ncols for r in t["rows"]):
                errors.append(f"{where}: table must have exactly {ncols} columns")
            elif len(t["rows"]) > pat["table"].get("max_rows", 10):
                errors.append(f"{where}: at most {pat['table'].get('max_rows', 10)} table rows")
            else:
                steps.append({"op": "set_table", "target": f"shape:{tsid}", "header": t["header"], "rows": t["rows"]})

        if "charts" in pat:
            specs = s.get("charts", {})
            unknown = set(specs) - set(pat["charts"])
            if unknown:
                errors.append(f"{where}: unknown chart slots {sorted(unknown)}; slots: {list(pat['charts'])}")
            for slot, sid in pat["charts"].items():
                used.add(sid)
                if slot not in specs:
                    steps.append({"op": "delete_shape", "target": f"shape:{sid}"})
                    continue
                step, errs = compile_chart(specs[slot], datasets, kit.get("chart_style", {}), f"{where} chart {slot!r}")
                errors += errs
                if step:
                    steps.append({**step, "target": f"shape:{sid}"})

        # leak guard: any example text the plan didn't claim is removed, and so is every picture
        # the kit doesn't explicitly keep (a logo is design; a screenshot or diagram is content).
        # An embedded object goes too: commercial templates carry add-in data blobs (think-cell
        # above all) on every slide, and a new deck should not inherit somebody else's.
        keep = set(pat.get("keep", []))
        for sid, shp in shapes.items():
            if sid in used or sid in keep or shp.get("kind") == "group":
                continue
            if _text_of(shp) or shp.get("kind") in ("picture", "ole") and sid not in keep:
                steps.append({"op": "delete_shape", "target": f"shape:{sid}"})

        ops.append({"op": "add_slide_from_pattern", "source": ex["ref"], "steps": steps, "notes": s.get("notes")})

    if errors:
        raise CompileError(errors)
    ops += [{"op": "delete_slide", "target": sl["ref"]} for sl in index["slides"]]   # drop all examples
    first = plan["slides"][0].get("fields", {}).get("title", "") if plan["slides"] else ""
    ops.append({"op": "set_doc_properties", "title": plan.get("title", first), "author": plan.get("author", "")})
    return ops


def example_texts(index: dict, kit: dict) -> set[str]:
    """Text from the example slides, used to prove none of it leaked into the new deck."""
    out = set()
    for p in kit["patterns"].values():
        for shp in index["slides"][p["slide"] - 1]["shapes"]:
            for para in shp.get("paras", []) + [q for c in shp.get("cells", []) for q in c["paras"]]:
                if len(para["text"].strip()) >= 12:
                    out.add(para["text"].strip())
    return out


def _strings(v):
    """Every string in a nested plan value."""
    if isinstance(v, str):
        yield v.strip()
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def leaked_text(index: dict, kit: dict, plan: dict, out_index: dict) -> list[str]:
    """Example content still present in the new deck: text (unless the plan itself supplied it)
    and pictures (by fingerprint, unless the kit keeps them as decoration)."""
    from .probe import iter_paragraphs
    out = {p["text"].strip() for p in iter_paragraphs(out_index)}
    leaks = sorted((example_texts(index, kit) - set(_strings(plan))) & out)
    kept, example_pics = set(), set()
    for p in kit["patterns"].values():
        # A picture the pattern itself uses is not a leak. Once slides with groups can be used
        # as patterns, an item is routinely "icon + heading + body" and the icon is a picture
        # inside the item group: it is meant to be copied, once per item. Only a picture that
        # belongs to the example slide and that no pattern claims is template content left
        # behind, which is what this scan is for.
        claimed = set(p["fields"].values()) | set(p.get("keep", []))
        if "items" in p:
            claimed |= {sid for group in p["items"]["groups"] for sid in group}
        claimed |= set(p.get("charts", {}).values())
        if "table" in p:
            claimed.add(p["table"].get("shape"))
        for shp in index["slides"][p["slide"] - 1]["shapes"]:
            if shp.get("image_sha"):
                sid = int(shp["ref"].rsplit("shape:", 1)[1])
                (kept if sid in claimed else example_pics).add(shp["image_sha"])
    out_pics = {shp.get("image_sha") for s in out_index["slides"] for shp in s["shapes"] if shp.get("image_sha")}
    leaks += [f"picture {h}" for h in sorted((example_pics - kept) & out_pics)]
    return leaks
