"""Build the test inputs.

sample.pptx  - 6 slides exercising mixed-format runs, a line break, a table, a
               group shape, speaker notes and a picture. Afterwards every XML part
               is re-serialized in a different style (pretty-printed, standalone
               decl) to mimic a deck authored in PowerPoint rather than python-pptx.
               That makes the byte-preservation test honest.
template.pptx - python-pptx's default template, standing in for a corporate template.
"""
import io
import sys
import zipfile

from lxml import etree
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_THEME_COLOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

OUT = sys.argv[1] if len(sys.argv) > 1 else "."

SAMPLE_TEXT = {
    "s1_title": "Q3 2026 Business Review",
    "s1_sub": "Global Markets\vInternal use only",
    "s2_title": "Q3 Results",
    "s2_bullets": [["Revenue grew ", "12%", " year on year"],
                   ["Cost-to-income ratio improved to 58%"],
                   ["Client franchise expanded across all regions"]],
    "s3_title": "Revenue by region",
    "s3_table": [["Region", "Q3 2026", "Change"], ["Europe", "€1.2bn", "+8%"], ["Asia Pacific", "€0.6bn", "+19%"]],
    "s4_title": "Strengths and risks",
    "s4_boxes": ["Strong fee income", "Rate volatility"],
    "s4_notes": "Emphasise the fee income story before discussing risks.",
    "s5_title": "Outlook for the fourth quarter",
    "s5_bullets": ["Deposit margins expected to stabilise as rate cuts slow",
                   "Pipeline in structured financing remains strong into year end",
                   "Cost discipline continues with technology investment protected",
                   "Capital ratios stay comfortably above regulatory requirements"],
    "s6_title": "Questions?",
    "s6_sub": "Contact: Strategy Team",
    "s7_title": "Order processing flow",
    "s7_cards": [("Receive", "Order arrives from the web shop or the partner API."),
                 ("Validate", "Address, payment method and items are checked."),
                 ("Reserve", "Stock is reserved in the nearest warehouse."),
                 ("Charge", "Payment is captured from the customer."),
                 ("Pick", "Items are picked from the shelves."),
                 ("Pack", "Items are packed and labelled."),
                 ("Ship", "Parcel is handed to the carrier."),
                 ("Notify", "Customer gets tracking details by email.")],
}


def build():
    prs = Presentation()
    L = prs.slide_layouts
    s = prs.slides.add_slide(L[0])
    s.shapes.title.text = SAMPLE_TEXT["s1_title"]
    s.placeholders[1].text = SAMPLE_TEXT["s1_sub"]

    s = prs.slides.add_slide(L[1])
    s.shapes.title.text = SAMPLE_TEXT["s2_title"]
    tf = s.placeholders[1].text_frame
    for i, runs in enumerate(SAMPLE_TEXT["s2_bullets"]):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        for j, t in enumerate(runs):
            r = p.add_run()
            r.text = t
            if j == 1:
                r.font.bold = True
                r.font.color.theme_color = MSO_THEME_COLOR.ACCENT_2

    s = prs.slides.add_slide(L[5])
    s.shapes.title.text = SAMPLE_TEXT["s3_title"]
    rows = SAMPLE_TEXT["s3_table"]
    tbl = s.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(8), Inches(2)).table
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            tbl.cell(r, c).text = v

    s = prs.slides.add_slide(L[5])
    s.shapes.title.text = SAMPLE_TEXT["s4_title"]
    grp = s.shapes.add_group_shape()
    for k, t in enumerate(SAMPLE_TEXT["s4_boxes"]):
        tb = grp.shapes.add_textbox(Inches(1 + 4.2 * k), Inches(2.5), Inches(3.6), Inches(1.2))
        tb.text_frame.text = t
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(24)
        tb.fill.solid()
        tb.fill.fore_color.rgb = RGBColor(0xE8, 0xF1, 0xEC)
    s.notes_slide.notes_text_frame.text = SAMPLE_TEXT["s4_notes"]

    s = prs.slides.add_slide(L[1])
    s.shapes.title.text = SAMPLE_TEXT["s5_title"]
    tf = s.placeholders[1].text_frame
    for i, t in enumerate(SAMPLE_TEXT["s5_bullets"]):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = t
        p.runs[0].font.size = Pt(32)

    s = prs.slides.add_slide(L[0])
    s.shapes.title.text = SAMPLE_TEXT["s6_title"]
    s.placeholders[1].text = SAMPLE_TEXT["s6_sub"]
    img = Image.new("RGB", (240, 120), (14, 95, 84))
    ImageDraw.Draw(img).text((20, 50), "logo", fill="white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    s.shapes.add_picture(buf, Inches(8), Inches(6.2), Inches(1.6))

    # Slide 7: 8 numbered "cards" in a 4x2 grid, each = circle + number + heading + description
    # as separate shapes - the pattern compile_card_grid recognises.
    s = prs.slides.add_slide(L[5])
    s.shapes.title.text = SAMPLE_TEXT["s7_title"]
    left, gutter, card_w, d = Inches(0.5), Inches(0.2), Inches(2.1), Inches(0.5)
    for i, (head, body) in enumerate(SAMPLE_TEXT["s7_cards"]):
        x = left + (i % 4) * (card_w + gutter)
        y = Inches(1.8) if i < 4 else Inches(4.3)
        circ = s.shapes.add_shape(MSO_SHAPE.OVAL, x, y, d, d)
        circ.fill.solid()
        circ.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_1
        circ.line.fill.background()
        for (sx, sy, sw, sh, text, size, bold) in [
                (x, y, d, d, str(i + 1), 16, True),
                (x + Inches(0.6), y, card_w - Inches(0.6), d, head, 15, True),
                (x, y + Inches(0.6), card_w, Inches(1.6), body, 11, False)]:
            tb = s.shapes.add_textbox(sx, sy, sw, sh)
            tb.text_frame.word_wrap = True
            tb.text_frame.text = text
            run = tb.text_frame.paragraphs[0].runs[0]
            run.font.size, run.font.bold = Pt(size), bold
            if text.isdigit():
                tb.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
                tb.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
                for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
                    setattr(tb.text_frame, side, 0)
    return prs


def powerpointify(src, dst):
    """Re-serialize all XML parts in a different (but equivalent) style."""
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in zi.namelist():
            d = zi.read(n)
            if n.endswith((".xml", ".rels")):
                root = etree.fromstring(d)
                d = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True, pretty_print=True)
            zo.writestr(n, d)


if __name__ == "__main__":
    build().save(f"{OUT}/_raw.pptx")
    powerpointify(f"{OUT}/_raw.pptx", f"{OUT}/sample.pptx")
    import os
    os.remove(f"{OUT}/_raw.pptx")
    Presentation().save(f"{OUT}/template.pptx")
    print("wrote sample.pptx, template.pptx")
