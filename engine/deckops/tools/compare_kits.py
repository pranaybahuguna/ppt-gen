"""Compare an automatically drafted kit with a reference (hand-written) kit, slide by slide.

    python3 tools/compare_kits.py template.pptx reference_kit.json

A pattern matches when, on the same example slide, the auto kit labels the same shapes:
same text fields (by shape id, names ignored), same repeated items with the same shape in
each item role, same chart areas, same table.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deckops.autokit import draft_kit  # noqa: E402


def _items(p):
    it = p.get("items")
    if not it:
        return None
    return frozenset(frozenset(g[pos] for g in it["groups"]) for pos in it["fields"].values())


def compare(template, ref_kit):
    auto = draft_kit(template, os.path.basename(template), dedupe=False)
    by_slide = {p["slide"]: p for p in auto["patterns"].values()}
    rows = []
    for name, ref in ref_kit["patterns"].items():
        a = by_slide.get(ref["slide"])
        if a is None:
            rows.append((name, ref["slide"], False, False, False, False, auto["notes"].get(str(ref["slide"]), ["not found"])))
            continue
        fields = set(ref["fields"].values()) == set(a["fields"].values())
        items = _items(ref) == _items(a)
        charts = set(ref.get("charts", {}).values()) == set(a.get("charts", {}).values())
        table = ref.get("table", {}).get("shape") == a.get("table", {}).get("shape")
        diff = []
        if not fields:
            diff.append(f"fields: ref {sorted(set(ref['fields'].values()))} auto {sorted(set(a['fields'].values()))}")
        if not items:
            diff.append("items differ")
        if not charts:
            diff.append(f"charts: ref {ref.get('charts')} auto {a.get('charts')}")
        rows.append((name, ref["slide"], fields, items, charts and table, fields and items and charts and table, diff))
    return auto, rows


if __name__ == "__main__":
    auto, rows = compare(sys.argv[1], json.load(open(sys.argv[2])))
    exact = sum(r[5] for r in rows)
    print(f"{'pattern':<10} {'slide':>5}  fields  items  charts/table  exact")
    for name, sl, f, i, c, e, diff in rows:
        print(f"{name:<10} {sl:>5}  {'yes' if f else 'NO':<6}  {'yes' if i else 'NO':<5}  {'yes' if c else 'NO':<12}  {'yes' if e else 'NO'}"
              + (f"   {'; '.join(diff)}" if diff else ""))
    print(f"\nexact match: {exact} of {len(rows)} patterns")
