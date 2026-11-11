"""A whole conversation about a deck, with no model involved.

    make demo

This walks one `deck_id` through every route the graph has - create, edit, translate, the
code escape hatch, undo - using the offline stub planner. It is the demo *and* the smoke test:
if this prints "demo complete" then the graph, the engine, the sandbox, Postgres and the object
store are all working together.

Two things it is honest about:

* the stub planner cannot *write* a deck's content (that needs a model), so stage 1 is supplied
  as a ready-made outline. Everything after that - choosing a layout per slide, compiling,
  applying, checking, repairing, rendering - is the real code path;
* the last slide of the outline is deliberately impossible: it asks for three charts, and no
  layout in the template has three chart areas. That is what sends it to the escape hatch,
  where generated code draws it instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from deck_agent import facade  # noqa: E402
from deck_agent.infra.storage import sha256_file, store  # noqa: E402
from deck_agent.llm.planner import StubAgentPlanner  # noqa: E402
from deck_agent.settings import Settings, reset, settings  # noqa: E402

def deck_id() -> str:
    """A fresh session per run, unless one is named.

    The state lives in Postgres, so re-running with the same id would find a deck already there
    and the router would - correctly - stop routing "create". A demo that only works once is a
    demo that lies, so each run gets its own session; `--id X` reuses one on purpose.
    """
    import time

    for i, arg in enumerate(sys.argv):
        if arg == "--id" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return "demo-" + time.strftime("%Y%m%d-%H%M%S")

CONTENT = {
    "facts": [
        {"id": "F1", "text": "4,100 support tickets arrived in September 2026", "source": "pilot report"},
        {"id": "F2", "text": "38% of incoming tickets were answered by the bot", "source": "pilot report"},
        {"id": "F3", "text": "median first response fell from 11 hours to 3 hours", "source": "pilot report"},
        {"id": "F4", "text": "the pilot covered two teams over eight weeks", "source": "pilot report"},
    ],
    "datasets": {"weekly_tickets": {"columns": ["week", "opened", "answered_by_bot"],
                                    "rows": [["W1", 980, 120], ["W2", 1010, 290],
                                             ["W3", 1040, 380], ["W4", 1070, 410]]}},
}

# Stage 1, supplied rather than written by a model (the stub cannot write content).
OUTLINE = {
    "title": "Support bot pilot",
    "footer": "Customer Operations - internal",
    "slides": [
        {"purpose": "name the deck", "kind": "cover", "title": "Support bot pilot",
         "subtitle": "Eight weeks, two teams, 4,100 tickets", "sources": ["F4"]},
        {"purpose": "show what changed", "kind": "numbers",
         "title": "What changed in eight weeks",
         "intro": "The bot answered a third of everything and the queue got shorter.",
         "points": [
             {"heading": "Answered by bot", "value": "38%", "text": "of incoming tickets", "fact": "F2"},
             {"heading": "First response", "value": "3h", "text": "down from 11 hours", "fact": "F3"},
             {"heading": "Tickets seen", "value": "4,100", "text": "in September", "fact": "F1"},
         ],
         "takeaway": "The queue is shorter and nobody was taken off the rota.", "sources": ["F1", "F2", "F3"]},
        {"purpose": "how it was rolled out", "kind": "sequence", "title": "How we rolled it out",
         "points": [
             {"heading": "Shadow", "text": "The bot drafted replies nobody sent."},
             {"heading": "Assist", "text": "Agents edited the drafts before sending."},
             {"heading": "Answer", "text": "The bot answered the clearest tickets itself."},
             {"heading": "Review", "text": "A weekly sample was read by a human."},
         ]},
        # Deliberately impossible: three charts, and no layout has three chart areas. This is
        # the slide that reaches the escape hatch.
        {"purpose": "force the escape hatch", "kind": "chart",
         "title": "Three views no layout holds",
         "charts": {"a": {"type": "column", "dataset": "weekly_tickets", "category": "week",
                          "series": ["opened"]},
                    "b": {"type": "line", "dataset": "weekly_tickets", "category": "week",
                          "series": ["answered_by_bot"]},
                    "c": {"type": "column", "dataset": "weekly_tickets", "category": "week",
                          "series": ["opened", "answered_by_bot"]}},
         "points": [
             {"heading": "Volume", "text": "Steady at about a thousand a week."},
             {"heading": "Deflection", "text": "Rising every week of the pilot."},
             {"heading": "Both", "text": "The gap is the agents' workload."},
         ]},
        {"purpose": "close", "kind": "closing", "title": "Next: both teams, all of October",
         "points": [{"heading": "Decision", "text": "Extend the pilot to the whole of support."}]},
    ],
}


def heading(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def show(result: dict) -> dict:
    keys = ("ok", "route", "deck", "versions", "warnings", "errors", "question", "reason")
    print(json.dumps({k: result[k] for k in keys if k in result}, indent=1, default=str))
    for entry in (result.get("changelog") or [])[:4]:
        print("   -", json.dumps(entry, default=str)[:150])
    return result


def main() -> int:
    global DECK
    DECK = deck_id()
    print(f"deck session: {DECK}")
    # The escape hatch is off by default; this demo exists partly to exercise it.
    reset(Settings.load())
    settings().allow_custom_code = True
    planner = StubAgentPlanner()

    template_path = REPO / "local" / "samples" / "example_style.pptx"
    if not template_path.exists():
        print(f"{template_path} is missing. Run: make samples")
        return 1
    template = store().put(template_path, f"uploads/{sha256_file(template_path)}-example_style.pptx")
    print(f"template in the bucket: {template}")

    heading("1. create - a new deck in the template's look")
    print("   the kit is built and cached on first use; the last slide cannot fit any layout")
    created = show(facade.create_deck(DECK, "the support bot pilot, for the operations review",
                                      CONTENT, template, planner=planner, outline=OUTLINE))
    if not created.get("deck"):
        print("\nthe deck was not built; stopping here")
        return 1

    heading("2. edit - change the deck that is now in this session")
    print("   the stub's editor just halves a title; a real model rewrites properly. What is")
    print("   being shown is the route, the fingerprinted ops and the byte-preserving save")
    show(facade.edit_deck(DECK, "make slide 3 less wordy", planner=planner))

    heading("3. translate - the same deck in French, slides 1 and 2")
    print("   the offline stub has a small French phrasebook, not a translator, so expect few")
    print("   or no ops here: the route, the fingerprints and the checks are what is being shown")
    show(facade.translate_deck(DECK, "fr", [1, 2], planner=planner))

    heading("4. ask - something we do not do; it says so plainly")
    show(facade.ask_deck(DECK, "restyle this onto the new corporate template", planner=planner))

    heading("5. ask - too vague, so the graph stops and asks")
    asked = show(facade.ask_deck(DECK, "do the thing with this deck", planner=planner))
    if asked.get("question"):
        print("\n   answering it, which resumes the run at the node that asked:")
        show(facade.answer(DECK, "translate it into German", planner=planner))

    heading("6. undo - drop the last version")
    print(json.dumps(facade.undo(DECK, planner=planner), indent=1, default=str))

    heading("7. the session, as a host agent would see it")
    state = facade.session(DECK, planner=planner)
    print(json.dumps({k: v for k, v in state.items() if k != "versions"}, indent=1, default=str))
    print("\nversions:")
    for i, uri in enumerate(state.get("versions") or [], start=1):
        print(f"   v{i:03d}  {uri}")

    heading("demo complete")
    print("Look at the deck:  .venv/bin/python -m deck_agent.cli get "
          f"{state.get('deck')} local/demo.pptx")
    print("Look at the previews in the bucket, or at the manifest, with the same command.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
