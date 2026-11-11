"""Print every shape on every slide with its id, position and text: what you need to write a
style kit for your own template (which shape id is the title, which ids make up one card...).

    python3 tools/inspect_deck.py my_template.pptx            # all slides
    python3 tools/inspect_deck.py my_template.pptx 2 5        # only slides 2 and 5

Runs locally on a template you trust. For untrusted uploads use the sandboxed probe.
"""
import sys

from pptx import Presentation

EMU_IN = 914400


def main(path, only=None):
    prs = Presentation(path)
    for n, s in enumerate(prs.slides, start=1):
        if only and n not in only:
            continue
        print(f"\n=== slide {n}  (layout: {s.slide_layout.name}, placeholders: {len(s.placeholders)})")
        for sh in s.shapes:
            kind = "table" if getattr(sh, "has_table", False) and sh.has_table else str(sh.shape_type).split(" ")[0]
            txt = sh.text_frame.text.replace("\n", " | ")[:60] if sh.has_text_frame else ""
            print(f"  id={sh.shape_id:<4} {kind:<12} x={sh.left / EMU_IN:5.2f} y={sh.top / EMU_IN:5.2f} "
                  f"w={sh.width / EMU_IN:5.2f} h={sh.height / EMU_IN:5.2f}  {txt}")


if __name__ == "__main__":
    main(sys.argv[1], {int(a) for a in sys.argv[2:]} or None)
