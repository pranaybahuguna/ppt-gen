"""End-to-end POC run.

1. sandbox self-test (network must be blocked inside)
2. translate slides 2-5 of sample.pptx to French (repair loop included)
3. prove slides 1 and 6 are byte-identical to the input
4. stale-fingerprint ops are refused, nothing written
5. zip bomb is rejected before any parser runs
6. create a 4-slide deck from the template
7. edit: 8 process cards -> 6, re-laid out as a 3x2 grid
8. create a new deck in the look of an example "house style" deck (copy example slides, swap content)

    python3 run_demo.py                          # offline stub planner
    python3 run_demo.py --llm-config llm.json    # real model; llm.json is git-ignored:
                                                 # {"base_url": "...", "model": "...", "api_key": "..."}
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from controlplane import orchestrator, sandbox                    # noqa: E402
from controlplane.planner import OpenAICompatPlanner, StubPlanner  # noqa: E402

OUT = os.path.join(ROOT, "out")
WS = os.path.join(OUT, "workspaces")


def hr(t):
    print(f"\n=== {t} " + "=" * (60 - len(t)))


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(WS)
    subprocess.run([sys.executable, os.path.join(ROOT, "samples/make_sample.py"), OUT], check=True)
    cfg = sys.argv[sys.argv.index("--llm-config") + 1] if "--llm-config" in sys.argv else None
    planner = OpenAICompatPlanner.from_file(cfg) if cfg else StubPlanner()
    results = {}

    hr("1. sandbox self-test")
    st = os.path.join(WS, "selftest")
    os.makedirs(st)
    rc, rep = sandbox.run(["selftest", "st.json"], st, os.path.join(st, "st.json"))
    print("  network inside sandbox:", rep["network"])
    results["network_blocked"] = rep["network"].startswith("blocked")

    hr("2. translate slides 2-5 -> French")
    run, man = orchestrator.translate(WS, os.path.join(OUT, "sample.pptx"), "fr", [2, 3, 4, 5], planner)
    for c in man["changelog"][:4]:
        print("   ", c)
    print(f"    ... {len(man['changelog'])} changes; parts: {man['exec']['parts']}")
    shutil.copy(run.p("out.pptx"), os.path.join(OUT, "sample_fr.pptx"))
    results["translate_overflow_left"] = len(man["remaining_findings"])
    results["translate_structural_ok"] = not man["structural"]

    hr("3. untouched slides byte-identical?")
    src, dst = zipfile.ZipFile(os.path.join(OUT, "sample.pptx")), zipfile.ZipFile(run.p("out.pptx"))
    for part in ["ppt/slides/slide1.xml", "ppt/slides/slide6.xml", "ppt/slideMasters/slideMaster1.xml",
                 "ppt/media/image1.png", "ppt/slides/slide2.xml"]:
        same = src.read(part) == dst.read(part)
        print(f"  {part:<40} {'IDENTICAL' if same else 'changed'}")
    results["untouched_identical"] = all(src.read(p) == dst.read(p) for p in ["ppt/slides/slide1.xml", "ppt/slides/slide6.xml"])

    hr("4. stale fingerprint is refused")
    ops = json.load(open(run.p("ops.json")))
    ops[0]["expect"]["text_sha"] = "0000000000"
    json.dump(ops, open(run.p("stale_ops.json"), "w"))
    rc, rep = sandbox.run(["execute", "source.pptx", "stale_ops.json", "stale.pptx", "stale.json"], run.ws, run.p("stale.json"))
    print(f"  rc={rc} ok={rep['ok']} errors={rep.get('errors')}  output written: {os.path.exists(run.p('stale.pptx'))}")
    results["stale_refused"] = rep["ok"] is False and not os.path.exists(run.p("stale.pptx"))

    hr("5. zip bomb rejected")
    bomb = os.path.join(run.ws, "bomb.pptx")
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("ppt/slides/slide1.xml", b"\0" * (600 * 1024 * 1024))
    print(f"  bomb on disk: {os.path.getsize(bomb) // 1024} KB")
    rc, rep = sandbox.run(["probe", "bomb.pptx", "bomb.json"], run.ws, run.p("bomb.json"))
    print(f"  rc={rc} -> {rep}")
    results["bomb_rejected"] = "rejected" in rep

    hr("6. create from template")
    crun, cman = orchestrator.create(WS, os.path.join(OUT, "template.pptx"), "fee compression in passive mandates", planner)
    shutil.copy(crun.p("out.pptx"), os.path.join(OUT, "created.pptx"))
    results["create_structural_ok"] = not cman["structural"]
    results["create_findings"] = len(cman["findings"])

    hr("7. edit: 8 cards -> 6")
    sys.path.insert(0, os.path.join(ROOT, "examples"))
    import edit_card_grid
    eman = edit_card_grid.main(os.path.join(OUT, "sample.pptx"), os.path.join(OUT, "sample_edited.pptx"))
    results["edit_only_slide7_changed"] = eman["exec"]["parts"]["changed"] == ["ppt/slides/slide7.xml"]
    results["edit_new_problems"] = len(eman["structural"]) + len(eman["findings"])

    hr("8. create a new deck in an example house style")
    subprocess.run([sys.executable, os.path.join(ROOT, "samples/make_style_template.py"), OUT], check=True)
    import create_from_style_kit
    krun, kman = create_from_style_kit.main(os.path.join(OUT, "example_style_kit.json"), os.path.join(OUT, "styled_deck.pptx"))
    results["styled_create_slides"] = kman["slides"]
    results["styled_create_problems"] = len(kman["structural"]) + len(kman["findings"])
    results["styled_create_leaked_text"] = len(kman["leaked_example_text"])

    hr("summary")
    for k, v in results.items():
        print(f"  {k:<28} {v}")
    json.dump({"results": results, "translate": man, "create": cman}, open(os.path.join(OUT, "summary.json"), "w"),
              indent=1, ensure_ascii=False)
    print("\n  previews:", man["previews"][0].rsplit("/", 2)[0], "|", cman["previews"][0].rsplit("/", 2)[0])


if __name__ == "__main__":
    main()
