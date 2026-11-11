"""Create a brand-new deck in the look of an example deck (see deckops/patterns.py).

    python3 samples/make_style_template.py out/        # example template + its style kit
    python3 examples/create_from_style_kit.py out/example_style_kit.json out/new_deck.pptx

PLAN is what the model returns when given the kit's pattern list and a content pack.
It's written by hand here so the example runs without a model. With a model configured,
pass plan=None and the planner produces it.
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from controlplane import orchestrator        # noqa: E402
from controlplane.planner import StubPlanner  # noqa: E402

# The content pack: what the agent layer gathered. Charts may only use these datasets.
CONTENT = {
    "facts": [{"id": "F1", "text": "The pilot covered order and returns questions for one region", "source": "pilot report"}],
    "datasets": {
        "weekly_tickets": {"source": "pilot report", "columns": ["Week", "Automated", "Handed to a person"],
                           "rows": [["W1", 210, 690], ["W2", 260, 640], ["W3", 330, 580], ["W4", 360, 560],
                                    ["W5", 390, 520], ["W6", 410, 500]]},
        "topics": {"source": "pilot report", "columns": ["Topic", "Share"],
                   "rows": [["Order status", 44], ["Returns", 31], ["Delivery", 17], ["Other", 8]]},
    },
}

PLAN = {
    "footer": "Support assistant pilot - project update",
    "slides": [
        {"pattern": "cover",
         "fields": {"title": "Support assistant", "subtitle": "Pilot results and next steps",
                    "tagline": "Answering routine customer questions automatically"},
         "items": [{"value": "38%", "label": "of tickets answered"}, {"value": "4 min", "label": "median reply time"},
                   {"value": "4.5/5", "label": "customer rating"}]},
        {"pattern": "columns",
         "fields": {"title": "Where we are", "subtitle": "Three months into the pilot"},
         "items": [{"heading": "Working", "points": ["Order status and returns handled end to end", "Hand-off to a person keeps context"]},
                   {"heading": "Not yet", "points": ["Billing disputes still go to people", "No support for voice channel"]}]},
        {"pattern": "steps",
         "fields": {"title": "How a ticket is handled", "subtitle": "From question to answer"},
         "items": [{"heading": "Receive", "body": "A customer message arrives by chat or email."},
                   {"heading": "Classify", "body": "The assistant decides if it can answer or should hand off."},
                   {"heading": "Answer", "body": "It replies using the help centre and order data."},
                   {"heading": "Escalate", "body": "Hard cases go to a person with a summary attached."},
                   {"heading": "Learn", "body": "Corrections from people improve the next answer."}]},
        {"pattern": "stats",
         "fields": {"title": "Pilot numbers", "subtitle": "Measured over twelve weeks",
                    "intro": "The pilot covered order and returns questions for one region.",
                    "panel_title": "Twelve-week results", "takeaway": "Next: extend to a second region in Q1."},
         "items": [{"label": "Tickets", "value": "11,200", "caption": "handled in total"},
                   {"label": "Automated", "value": "38%", "caption": "closed without a person"},
                   {"label": "Escalations", "value": "6%", "caption": "re-opened after hand-off"},
                   {"label": "Rating", "value": "4.5", "caption": "average out of 5"}]},
        {"pattern": "chart",
         "fields": {"title": "Automation is rising", "subtitle": "Tickets per week, by who closed them",
                    "commentary_title": "What it shows",
                    "commentary": ["Automated answers doubled in six weeks", "Hand-offs to people fell by a quarter"],
                    "takeaway": "Next: add billing questions."},
         "charts": {"main": {"type": "stacked_column", "dataset": "weekly_tickets", "category": "Week",
                             "series": ["Automated", "Handed to a person"], "number_format": "#,##0"}}},
        {"pattern": "two_charts",
         "fields": {"title": "What customers ask", "subtitle": "Topics and weekly automation",
                    "left_caption": "Questions by topic", "right_caption": "Automated tickets per week",
                    "takeaway": "Order status and returns are three quarters of all questions."},
         "charts": {"left": {"type": "pie", "dataset": "topics", "category": "Topic", "series": ["Share"]},
                    "right": {"type": "line", "dataset": "weekly_tickets", "category": "Week", "series": ["Automated"]}}},
        {"pattern": "table",
         "fields": {"title": "Open risks", "subtitle": "What we are watching"},
         "table": {"header": ["Risk", "Mitigation"],
                   "rows": [["Wrong answer on refunds", "Refund answers always quote the policy page"],
                            ["Customer wants a person", "One-click hand-off on every reply"],
                            ["Peak volume", "Queue limit with fallback to people"]]}},
        {"pattern": "panels",
         "fields": {"title": "Summary", "subtitle": "What we ask for", "takeaway": "Decision: approve the second region."},
         "items": [{"label": "The ask", "text": "Extend the pilot to a second region for one quarter."},
                   {"label": "Outcome", "text": "Faster answers for customers and fewer routine tickets for the team."}]},
        {"pattern": "closing", "fields": {"title": "Thank you", "subtitle": "Questions welcome",
                                          "note": "Next review: end of Q1."}},
    ],
}


def main(kit_path, dst):
    run, man = orchestrator.create_from_kit(os.path.join(ROOT, "out", "workspaces"), kit_path,
                                            "pilot update deck", CONTENT, StubPlanner(), plan=PLAN)
    shutil.copy(run.p("out.pptx"), dst)
    print(json.dumps({k: man[k] for k in ("slides", "ops", "findings", "structural", "leaked_example_text")}, indent=1))
    return run, man


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
