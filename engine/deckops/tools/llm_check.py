"""One command to validate deckops against a real model, and package the results.

    python3 tools/llm_check.py my_template.pptx llm.json [content.json]

Runs: (1) build the style kit with vision labels, (2) create a deck in two stages from the
content pack (default: samples/content_example.json). Then zips the run folders' JSON reports,
slide previews and the deck into llm_check_results.zip. llm.json (the key) is never included.
"""
import glob
import json
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from controlplane import orchestrator  # noqa: E402
from controlplane.planner import OpenAICompatPlanner  # noqa: E402


def main(template, llm_path, content_path=None, out_dir=None):
    out_dir = out_dir or os.path.join(ROOT, "runs", "llm_check_" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    planner = OpenAICompatPlanner.from_file(llm_path)
    content = json.load(open(content_path or os.path.join(ROOT, "samples", "content_example.json")))
    summary = {"model": planner.model, "template": os.path.basename(template)}

    print("1/2 building the style kit (rules + vision labels + tests) ...")
    t = time.time()
    run_k, kit, report = orchestrator.prepare_template(out_dir, template, planner)
    kit_path = os.path.join(out_dir, "kit.json")
    kit["template"] = os.path.relpath(os.path.abspath(template), out_dir)
    json.dump(kit, open(kit_path, "w"), indent=1)
    labels = json.load(open(run_k.p("label_decisions.json")))
    summary["kit"] = {"seconds": round(time.time() - t), "patterns": sorted(kit["patterns"]),
                      "vision_answered": sum(v["vision_used"] for v in labels.values()),
                      "vision_changes": sum(len(v["vision"]) for v in labels.values()),
                      "vision_vetoed": sum(len(v["vetoed"]) for v in labels.values())}

    print("2/2 creating a deck in two stages ...")
    t = time.time()
    run_c, man = orchestrator.create_from_kit(out_dir, kit_path, "a short deck on these results", content, planner)
    attempts = json.load(open(run_c.p("render_attempts.json")))
    summary["create"] = {"seconds": round(time.time() - t), "slides": man["slides"],
                         "retries": sum(len(a) - 1 for a in attempts), "repairs": man["repairs"],
                         "findings": len(man["findings"]), "leaked": len(man["leaked_example_text"])}
    json.dump(summary, open(os.path.join(out_dir, "summary.json"), "w"), indent=1)

    zpath = os.path.join(ROOT, "llm_check_results.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in glob.glob(os.path.join(out_dir, "**", "*"), recursive=True):
            if f.endswith((".json", ".png")) or f.endswith("out.pptx"):
                if "llm.json" not in f and "/.home/" not in f:
                    z.write(f, os.path.relpath(f, out_dir))
    print(json.dumps(summary, indent=1))
    print(f"\nsend this file back: {zpath}")
    return summary


if __name__ == "__main__":
    main(*sys.argv[1:4])
