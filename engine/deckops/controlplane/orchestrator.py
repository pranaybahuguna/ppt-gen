"""Control plane: the pipeline. Never opens a .pptx itself - every byte-touching
step goes through sandbox.run(). Holds only JSON (index, plan, ops, reports).

Stages: intake -> probe -> plan -> compile -> execute -> validate -> repair -> deliver
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid

from deckops.compiler import compile_create, compile_translate
from deckops.probe import iter_paragraphs

from . import sandbox

MAX_REPAIR = 2


class Run:
    def __init__(self, root, planner, log=print):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.ws = os.path.abspath(os.path.join(root, self.id))
        os.makedirs(self.ws)
        self.planner, self.log, self.trace = planner, log, []

    def p(self, name):
        return os.path.join(self.ws, name)

    def save(self, name, obj):
        json.dump(obj, open(self.p(name), "w"), indent=1, ensure_ascii=False)

    def box(self, *args):
        """Run a data-plane command in the sandbox; report path is always the last arg."""
        t0 = time.time()
        rc, rep = sandbox.run(list(args), self.ws, self.p(args[-1]))
        self.trace.append({"stage": args[0], "rc": rc, "seconds": round(time.time() - t0, 2)})
        self.log(f"  [sandbox] {args[0]:<8} rc={rc} {round(time.time() - t0, 1)}s")
        if rc not in (0, 3):
            raise sandbox.SandboxError(json.dumps(rep)[:1500])
        return rc, rep


def _changelog(index, ops):
    paras = {p["ref"]: (s["index"], p["text"]) for s in index["slides"] for p in iter_paragraphs({"slides": [s]})}
    out = []
    for op in ops:
        if op["op"] == "set_paragraph_text":
            from deckops.refs import parse_tagged
            toks = parse_tagged(op["value"])
            after = "".join(t[2] if t[0] == "r" else (t[1] if t[0] == "plain" else "\n" if t[0] == "br" else "") for t in toks)
            si, before = paras[op["target"]]
            out.append({"slide": si + 1, "ref": op["target"], "before": before, "after": after})
        else:
            out.append({k: v for k, v in op.items() if k != "expect"})
    return out


def translate(root, src_path, lang, slide_numbers, planner, log=print):
    run = Run(root, planner, log)
    log(f"run {run.id}  translate -> {lang}, slides {slide_numbers}")
    shutil.copy(src_path, run.p("source.pptx"))                                  # 0 intake
    _, index = run.box("probe", "source.pptx", "index.json")                      # 1 probe
    scope = {n - 1 for n in slide_numbers}
    paras = list(iter_paragraphs(index, scope))
    log(f"  probe: {len(index['slides'])} slides, {len(paras)} paragraphs in scope, "
        f"{len(index['unsupported'])} opaque objects, {len(index['external_rels'])} external links")
    plan = planner.translate(paras, lang)                                         # 2-4 plan
    run.save("plan.json", plan)
    ops = compile_translate(plan, index)                                          # 5 compile (pure)
    log(f"  compile: {len(ops)} ops")
    by_ref = {p["ref"]: p for p in paras}
    # Differential validation: the source is the baseline. Only flag shapes the edit
    # made worse - the heuristic is approximate and many real decks already "overflow".
    scope_arg = ",".join(map(str, sorted(scope)))
    _, base = run.box("validate", "source.pptx", scope_arg, "-", "baseline.json")
    base_ratio = {f["ref"]: f["ratio"] for f in base["findings"] if f["kind"] == "overflow"}
    log(f"  baseline: {len(base_ratio)} shapes already tight in the source")
    for attempt in range(MAX_REPAIR + 1):
        run.save("ops.json", ops)
        rc, ex = run.box("execute", "source.pptx", "ops.json", "out.pptx", "exec.json")   # 6 execute
        if rc == 3:
            raise RuntimeError(f"execution refused: {ex['errors']}")
        _, val = run.box("validate", "out.pptx", ",".join(map(str, sorted(scope))),
                         f"render{attempt}", "validate.json")                     # 7 validate
        over = [f for f in val["findings"] if f["kind"] == "overflow"
                and f["ratio"] > max(1.0, base_ratio.get(f["ref"], 0)) * 1.03]
        val["overlaps"] = [f for f in val["findings"] if f["kind"] == "overlap"]
        val["findings"] = over
        log(f"  validate (attempt {attempt}): {len(over)} new overflow"
            f"{' (template has autofit: PowerPoint may shrink it on edit, LibreOffice will not)' if any(f['masked_by_autofit'] for f in over) else ''}"
            f", structural={val['structural'] or 'ok'}")
        if not over or attempt == MAX_REPAIR:
            break
        # 8 repair: first ask the model for shorter phrasing, then deterministic font fallback
        current = {o["target"]: o["value"] for o in ops if o["op"] == "set_paragraph_text"}
        if attempt == 0:
            victims = [dict(by_ref[r], tagged=current.get(r, by_ref[r]["tagged"]))
                       for f in over for r in by_ref if r.startswith(f["ref"] + "/")]
            short = planner.shorten(victims, lang, 25)
            plan_map = {p["ref"]: p for p in plan["paragraphs"]}
            for p in short["paragraphs"]:
                plan_map[p["ref"]] = p
            plan["paragraphs"] = list(plan_map.values())
            ops = compile_translate(plan, index)
            log(f"  repair: model shortened {len(short['paragraphs'])} paragraphs")
        else:
            ops = [o for o in ops if not (o["op"] == "set_font_size" and o["target"] in {f["ref"] for f in over})]
            ops += [{"op": "set_font_size", "target": f["ref"], "size_pt": f["suggest_size_pt"]} for f in over]
            log(f"  repair: font size fallback on {len(over)} shapes")
    manifest = {
        "run": run.id, "mode": "translate", "planner": planner.name, "target_lang": lang,
        "ops": len(ops), "exec": ex, "remaining_findings": val["findings"],
        "structural": val["structural"], "previews": val.get("previews"),
        "changelog": _changelog(index, ops), "trace": run.trace,
    }
    run.save("manifest.json", manifest)                                           # 9 deliver
    return run, manifest


def create(root, template_path, brief, planner, log=print, family_map=None):
    run = Run(root, planner, log)
    log(f"run {run.id}  create from template")
    shutil.copy(template_path, run.p("source.pptx"))
    _, index = run.box("probe", "source.pptx", "index.json")
    plan = planner.plan_create(brief)
    run.save("plan.json", plan)
    ops = compile_create(plan, index["layouts"], family_map)
    ops += [{"op": "delete_slide", "target": s["ref"]} for s in index["slides"]]  # template sample slides
    log(f"  compile: {len(ops)} ops -> layouts {[o['layout'] for o in ops if o['op'] == 'add_slide']}")
    run.save("ops.json", ops)
    rc, ex = run.box("execute", "source.pptx", "ops.json", "out.pptx", "exec.json")
    if rc == 3:
        raise RuntimeError(f"execution refused: {ex['errors']}")
    _, val = run.box("validate", "out.pptx", "all", "render0", "validate.json")
    log(f"  validate: {len(val['findings'])} findings, structural={val['structural'] or 'ok'}")
    manifest = {"run": run.id, "mode": "create", "planner": planner.name, "ops": len(ops), "exec": ex,
                "findings": val["findings"], "structural": val["structural"],
                "previews": val.get("previews"), "changelog": _changelog(index, ops), "trace": run.trace}
    run.save("manifest.json", manifest)
    return run, manifest


def edit(root, src_path, plan, compile_fn, slide_numbers, planner_name, log=print):
    """Edit mode: plan (from the model) -> compile_fn -> sandboxed execute/validate."""
    run = Run(root, None, log)
    log(f"run {run.id}  edit slides {slide_numbers}")
    shutil.copy(src_path, run.p("source.pptx"))
    _, index = run.box("probe", "source.pptx", "index.json")
    run.save("plan.json", plan)
    ops = compile_fn(plan, index)
    log(f"  compile: {len(ops)} ops " + str({k: sum(o['op'] == k for o in ops) for k in {o['op'] for o in ops}}))
    run.save("ops.json", ops)
    scope = ",".join(str(n - 1) for n in slide_numbers)
    _, base = run.box("validate", "source.pptx", scope, "-", "baseline.json")
    base_ratio = {f["ref"]: f["ratio"] for f in base["findings"] if f["kind"] == "overflow"}
    base_overlaps = {(f["slide_index"], tuple(f["shapes"])) for f in base["findings"] if f["kind"] == "overlap"}
    rc, ex = run.box("execute", "source.pptx", "ops.json", "out.pptx", "exec.json")
    if rc == 3:
        raise RuntimeError(f"execution refused: {ex['errors']}")
    _, val = run.box("validate", "out.pptx", scope, "render0", "validate.json")
    over = [f for f in val["findings"]
            if (f["kind"] == "overflow" and f["ratio"] > max(1.0, base_ratio.get(f["ref"], 0)) * 1.03)
            or (f["kind"] == "overlap" and (f["slide_index"], tuple(f["shapes"])) not in base_overlaps)]
    val["structural"] = [p for p in val["structural"] if p not in base["structural"]]   # only what WE broke
    log(f"  validate: {len(over)} new overflow, new structural problems={val['structural'] or 'none'}"
        f" (pre-existing in source: {base['structural'] or 'none'})")
    manifest = {"run": run.id, "mode": "edit", "planner": planner_name, "ops": len(ops), "exec": ex,
                "findings": over, "structural": val["structural"], "previews": val.get("previews"),
                "changelog": _changelog(index, ops), "trace": run.trace}
    run.save("manifest.json", manifest)
    return run, manifest


def create_from_kit(root, kit_path, brief, content, planner, log=print, plan=None, outline=None,
                    retries=2, two_stage=True):
    """Create a new deck in the look of an example deck (see deckops/patterns.py).

    content: the "content pack" - facts the deck may use, gathered by the agent layer.
    plan:    optional ready-made plan (skips the model; used for tests and hand-written decks).
    outline: optional ready-made stage-1 content (skips the outline call; stage 2 still runs).
    Otherwise two stages (controlplane/slide_flow.py): the model writes the content slide by slide,
    then fills one layout per slide; compile errors and text that doesn't fit go back to the model
    for that slide only, up to `retries` times.
    """
    from deckops.patterns import compile_from_kit, describe_kit, leaked_text, load_kit
    from . import slide_flow
    kit = load_kit(kit_path)
    run = Run(root, planner, log)
    log(f"run {run.id}  create from style kit '{kit.get('name', '?')}'")
    tpl = os.path.join(os.path.dirname(os.path.abspath(kit_path)), kit["template"])
    shutil.copy(tpl, run.p("source.pptx"))                                      # 0 intake
    _, index = run.box("probe", "source.pptx", "index.json")                     # 1 survey the example deck
    log(f"  probe: {len(index['slides'])} example slides, {len(kit['patterns'])} patterns in kit")
    jobs = None
    if plan is None and (outline is not None or two_stage):                      # 2 plan: two stages
        if outline is None:
            outline = planner.outline(brief, content, slide_flow.max_points(kit))
        run.save("outline.json", outline)
        plan, jobs, attempts = slide_flow.plan_slides(outline, kit, index, content, planner, retries, log)
        run.save("render_attempts.json", attempts)
        log(f"  stage 2: {len(plan['slides'])} slides rendered, "
            f"{sum(len(a) - 1 for a in attempts)} retried after an error")
    elif plan is None:                                                           # 2 plan: one call
        plan = planner.plan_from_kit(brief, describe_kit(kit), content)

    ex_slides = sorted({p["slide"] - 1 for p in kit["patterns"].values()})
    _, base = run.box("validate", "source.pptx", ",".join(map(str, ex_slides)), "-", "baseline.json")
    base_ratio = {(f["slide_index"], f["ref"].rsplit("shape:", 1)[1]): f["ratio"]
                  for f in base["findings"] if f["kind"] == "overflow"}
    base_overlaps = {(f["slide_index"], tuple(f["shapes"])) for f in base["findings"] if f["kind"] == "overlap"}

    def build(plan, tag):
        run.save("plan.json", plan)
        ops = compile_from_kit(plan, index, kit, content)                        # 3 work orders (pure)
        log(f"  compile{tag}: {len(plan['slides'])} slides -> {len(ops)} ops "
            f"({sum(len(o.get('steps', [])) for o in ops)} steps on copied slides)")
        run.save("ops.json", ops)
        rc, ex = run.box("execute", "source.pptx", "ops.json", "out.pptx", "exec.json")   # 4 build
        if rc == 3:
            raise RuntimeError(f"execution refused: {ex['errors']}")
        _, val = run.box("validate", "out.pptx", "all", "render0", "validate.json")      # 5 check

        def example_slide(f):
            return kit["patterns"][plan["slides"][f["slide_index"]]["pattern"]]["slide"] - 1

        def keep(f):
            if f["kind"] == "overlap":       # new overlap = not already present on the example slide
                return (example_slide(f), tuple(f["shapes"])) not in base_overlaps
            return f["ratio"] > 1.03 * max(1.0, base_ratio.get((example_slide(f), f["ref"].rsplit("shape:", 1)[1]), 0))
        val["findings"] = [f for f in val["findings"] if keep(f)]
        _, out_index = run.box("probe", "out.pptx", "out_index.json")
        return ops, ex, val, out_index

    ops, ex, val, out_index = build(plan, "")
    repairs = []
    for round_ in range(1, retries + 1):                                         # 6 send problems back
        if not (val["findings"] and jobs):
            break
        texts = {sh["ref"]: " ".join(p["text"] for p in sh.get("paras", []))
                 for sl in out_index["slides"] for sh in sl["shapes"]}
        by_slide = {}
        for f in val["findings"]:
            if f["kind"] == "overflow":
                # text wraps by whole lines, so ask for a length that fits the lines the box holds
                text = texts.get(f["ref"], "")
                line = 1.2 * (f.get("max_font_pt") or 12)
                fits, needs = max(1, int(f["available_pt"] // line)), max(1.0, f["needed_pt"] / line)
                limit = max(8, int(len(text) * fits / needs * 0.9))
                msg = f'text too long for its box: "{text[:160]}"; rewrite it in at most {limit} characters'
            else:
                msg = "two text boxes overlap: use shorter text or fewer items"
            by_slide.setdefault(f["slide_index"], []).append(msg)
        log(f"  repair round {round_}: slides {[i + 1 for i in by_slide]} sent back with what to fix")
        for i, msgs in by_slide.items():
            ctx = {"deck_title": plan.get("title", ""), "slide": i + 1, "of": len(plan["slides"])}
            fixed, att = slide_flow.render_one(planner, jobs[i], ctx, index, kit, content, retries,
                                               feedback=msgs, previous=plan["slides"][i], log=log)
            repairs.append({"round": round_, "slide": i + 1, "feedback": msgs, "attempts": att})
            if fixed:
                plan["slides"][i] = fixed
        ops, ex, val, out_index = build(plan, f" (repair {round_})")
    if repairs:
        run.save("repairs.json", repairs)
    leaked = leaked_text(index, kit, plan, out_index)
    log(f"  validate: {len(val['findings'])} overflow, structural={val['structural'] or 'ok'}, "
        f"example text leaked: {len(leaked)}")
    manifest = {"run": run.id, "mode": "create_from_kit", "kit": kit.get("name"), "planner": planner.name,
                "slides": len(plan["slides"]), "ops": len(ops), "exec": ex, "findings": val["findings"],
                "structural": val["structural"], "leaked_example_text": leaked, "repairs": len(repairs),
                "previews": val.get("previews"), "trace": run.trace}
    run.save("manifest.json", manifest)
    return run, manifest


def prepare_template(root, template_path, planner, log=print):
    """Template in, tested style kit out. No person involved.

    1 sandbox: rules draft a pattern for every usable slide (structure: boxes, repeats, groups)
    2 check every id exists
    3 sandbox: render each candidate slide, and a copy with every box outlined and numbered
    4 control plane, optional: the vision model labels each numbered box and names the slide kind;
      kit_merge marries the two (structure from rules, meaning from vision, physical vetoes)
    5 keep the first slide of each kind; acceptance test (build + check test slides) on the rest
    6 measure how many items each pattern really holds (more than the example shows, if they fit)
    """
    from deckops.patterns import validate_kit
    from .kit_check import acceptance_test, measure_capacity
    from .kit_merge import boxes_for_vision, merge
    run = Run(root, planner, log)
    log(f"run {run.id}  prepare template {os.path.basename(template_path)}")
    shutil.copy(template_path, run.p("source.pptx"))
    _, draft = run.box("autokit", "source.pptx", "--all", "draft_kit.json")          # 1 rules (sandbox)
    _, index = run.box("probe", "source.pptx", "index.json")
    total = draft.get("slides_total", len(index["slides"]))
    log(f"  draft: {len(draft['patterns'])} candidate slides")
    kit = {k: v for k, v in draft.items() if k not in ("notes", "slides_total")}
    bad = validate_kit(kit, index)                                                    # 2 ids exist?
    for name in {e.split("'")[1] for e in bad if "'" in e}:
        kit["patterns"].pop(name, None)
    cands = kit["patterns"]
    slides = sorted(p["slide"] - 1 for p in cands.values())
    run.box("validate", "source.pptx", ",".join(map(str, slides)), "examples", "examples.json")   # 3 pictures
    targets = {}
    for p in cands.values():
        loose = list(p["fields"].values())
        first = [p["items"]["groups"][0][pos] for pos in p["items"]["fields"].values()] if "items" in p else []
        targets[str(p["slide"])] = {"ids": loose + first, "item_ids": first}
    run.save("mark_targets.json", targets)
    run.box("marks", "source.pptx", "examples", "mark_targets.json", "marks.json")
    png = lambda p, sfx="": open(run.p(f"examples/slide-{p['slide']:02d}{sfx}.png"), "rb").read()

    vision = getattr(planner, "vision", False)                                        # 4 vision + merge
    merged, decisions, answers = {}, {}, {}
    for key, p in sorted(cands.items(), key=lambda kv: kv[1]["slide"]):
        n = p["slide"]
        label = None
        if vision:
            try:
                label = planner.label_slide(png(p, "-marks"), boxes_for_vision(p, index["slides"][n - 1]),
                                            key.split("@")[0], slide=n)
            except Exception as e:
                log(f"  vision failed on slide {n}: {e.__class__.__name__}; using the rules alone")
        if label:
            answers[str(n)] = label
        kind, mp, dec = merge(key, p, index["slides"][n - 1], label, n, total)
        decisions[str(n)] = {"rules": key.split("@")[0], "final": kind, "vision_used": bool(label), **dec}
        if kind is None:
            continue
        if kind in merged:                                                            # 5 first of each kind
            decisions[str(n)]["dropped"] = f"same kind as slide {merged[kind]['slide']} ({kind})"
            continue
        mp.pop("big_text", None)
        merged[kind] = mp
    kit["patterns"] = merged
    kit["template"] = "source.pptx"
    run.save("merged_kit.json", kit)
    run.save("label_decisions.json", decisions)
    if answers:
        run.save("vision_labels.json", answers)        # raw answers: replay with --labels, no model needed
    pngs = {name: png(p) for name, p in merged.items()}
    final, report = acceptance_test(root, run.p("merged_kit.json"), index, planner, log, pngs)
    run.save("accepted_kit.json", final)
    final, capacity = measure_capacity(root, run.p("accepted_kit.json"), index, planner, log)   # 6 how many fit
    log("  capacity: " + ", ".join(f"{n} {r['example']}->{r['max']}" for n, r in capacity.items()))
    run.save("kit.json", final)
    run.save("kit_report.json", {"draft_notes": draft.get("notes"), "id_problems": bad,
                                 "labels": decisions, "patterns": report, "capacity": capacity})
    passed = sorted(final["patterns"])
    log(f"  kit: {len(passed)} of {len(merged)} patterns passed: {passed}"
        + ("" if vision else "  (rules only: no vision model)"))
    return run, final, report
