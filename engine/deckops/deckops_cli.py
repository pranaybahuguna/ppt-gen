"""Command line for the three everyday jobs.

    # 1. once per template: build and test its style kit (no person involved)
    python3 deckops_cli.py kit my_template.pptx my_kit.json                  # rules only
    python3 deckops_cli.py kit my_template.pptx my_kit.json --llm llm.json   # rules + vision model

    # 2. create a new deck in that template's look (two stages: content, then a layout per slide)
    python3 deckops_cli.py create my_kit.json content.json "brief for the deck" new_deck.pptx --llm llm.json
    python3 deckops_cli.py create my_kit.json content.json "brief" new.pptx --outline outline.json  # no model
    python3 deckops_cli.py create my_kit.json content.json "brief" new_deck.pptx --plan plan.json   # no model

    # 3. translate slides of an existing deck
    python3 deckops_cli.py translate deck.pptx fr 1,2,5 deck_fr.pptx --llm llm.json

Without --llm, an offline stub planner is used (it only knows the demo's French strings).
llm.json: {"base_url": "https://...", "model": "...", "api_key": "..."}  (keep it out of git).
Every run leaves its files (plans, ops, reports, slide previews) in runs/<id>/.
"""
import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from controlplane import orchestrator  # noqa: E402
from controlplane.planner import OpenAICompatPlanner, RecordedVision, StubPlanner  # noqa: E402


def planner(args):
    if getattr(args, "labels", None):
        return RecordedVision(args.labels)
    return OpenAICompatPlanner.from_file(args.llm) if args.llm else StubPlanner()


def cmd_kit(args):
    run, kit, report = orchestrator.prepare_template(args.runs, args.template, planner(args))
    # the kit points at its template relative to where the kit is saved
    kit["template"] = os.path.relpath(os.path.abspath(args.template), os.path.dirname(os.path.abspath(args.kit_out)))
    json.dump(kit, open(args.kit_out, "w"), indent=1)
    shutil.copy(run.p("kit_report.json"), os.path.splitext(args.kit_out)[0] + "_report.json")
    passed = [n for n, r in report.items() if not n.startswith("_") and r.get("passed")]
    failed = {n: r.get("reasons") for n, r in report.items() if not n.startswith("_") and not r.get("passed")}
    print(f"\nkit: {args.kit_out}  ({len(passed)} patterns: {', '.join(passed)})")
    for n, why in failed.items():
        print(f"  dropped {n}: {why}")
    print(f"test deck: {report['_test_deck']}")


def cmd_create(args):
    content = json.load(open(args.content))
    plan = json.load(open(args.plan)) if args.plan else None
    outline = json.load(open(args.outline)) if args.outline else None
    if plan is None and outline is None and not args.llm:
        sys.exit("create needs --llm llm.json, or --outline outline.json (stage 2 offline), or --plan plan.json")
    run, man = orchestrator.create_from_kit(args.runs, args.kit, args.brief, content, planner(args),
                                            plan=plan, outline=outline, two_stage=not args.single_call)
    shutil.copy(run.p("out.pptx"), args.out)
    print(json.dumps({k: man.get(k) for k in ("slides", "findings", "structural", "leaked_example_text", "repairs")},
                     indent=1))
    print(f"\ndeck: {args.out}   previews: {run.p('render1')}")


def cmd_translate(args):
    slides = [int(x) for x in args.slides.split(",")]
    run, man = orchestrator.translate(args.runs, args.deck, args.lang, slides, planner(args))
    shutil.copy(run.p("out.pptx"), args.out)
    print(f"\ndeck: {args.out}   run folder: {run.ws}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default=os.path.join(ROOT, "runs"), help="where run folders go")
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("kit", help="build and test a style kit for a template")
    k.add_argument("template")
    k.add_argument("kit_out")
    k.add_argument("--llm", help="optional: vision-capable model that labels each box and checks test slides")
    k.add_argument("--labels", help="optional: replay a run's vision_labels.json instead of calling a model")
    k.set_defaults(fn=cmd_kit)

    c = sub.add_parser("create", help="create a deck from a style kit")
    c.add_argument("kit")
    c.add_argument("content", help="content pack JSON: facts and datasets the deck may use")
    c.add_argument("brief")
    c.add_argument("out")
    c.add_argument("--llm")
    c.add_argument("--plan", help="use this plan instead of asking a model")
    c.add_argument("--outline", help="use this stage-1 content; stage 2 (layout per slide) still runs")
    c.add_argument("--single-call", action="store_true", help="old mode: one model call plans the whole deck")
    c.set_defaults(fn=cmd_create)

    t = sub.add_parser("translate", help="translate slides of a deck")
    t.add_argument("deck")
    t.add_argument("lang")
    t.add_argument("slides", help="comma-separated slide numbers, e.g. 1,2,5")
    t.add_argument("out")
    t.add_argument("--llm")
    t.set_defaults(fn=cmd_translate)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
