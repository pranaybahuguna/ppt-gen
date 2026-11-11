"""Edit mode example: slide 7 of samples/sample.pptx has 8 numbered process cards.
Merge them into 6 cards with fuller descriptions, re-laid out as a 3x2 grid.

The plan below is what the model would return. It names cards by number and gives
text - no coordinates. compile_card_grid computes every position from the slide's
own grid (margins, gutters, rows).

    python3 run_demo.py                       # builds out/sample.pptx first
    python3 examples/edit_card_grid.py out/sample.pptx out/sample_edited.pptx
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from controlplane import orchestrator          # noqa: E402
from deckops.compiler import compile_card_grid  # noqa: E402

CARDS = [
    {"from": [1, 2], "heading": "Receive & validate",
     "body": "Orders arrive from the web shop or the partner API. Address, payment method and items "
             "are checked before anything is reserved."},
    {"from": [3], "heading": "Reserve",
     "body": "Stock is reserved in the nearest warehouse that can fill the whole order, so it ships in one parcel."},
    {"from": [4], "heading": "Charge",
     "body": "Payment is captured only after stock is secured, which avoids refunds for items we can't send."},
    {"from": [5, 6], "heading": "Pick & pack",
     "body": "Items are picked from the shelves, packed and labelled in a single pass at the packing station."},
    {"from": [7], "heading": "Ship",
     "body": "The parcel is handed to the carrier and the tracking number is stored against the order."},
    {"from": [8], "heading": "Notify",
     "body": "The customer gets tracking details by email, and again when the carrier confirms delivery."},
]


def main(src, dst):
    # The control plane never opens the deck: the compiler resolves "slide 7" against the
    # DeckIndex that the sandboxed probe produced.
    plan = {"mode": "edit", "slide_number": 7, "columns": 3, "cards": CARDS}
    run, man = orchestrator.edit(os.path.join(ROOT, "out", "workspaces"), src, plan, compile_card_grid, [7],
                                 "hand-written plan (stands in for the model)")
    shutil.copy(run.p("out.pptx"), dst)
    print(json.dumps({k: man[k] for k in ("ops", "findings", "structural", "previews")}, indent=1))
    print("parts changed:", man["exec"]["parts"]["changed"])
    return man


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
