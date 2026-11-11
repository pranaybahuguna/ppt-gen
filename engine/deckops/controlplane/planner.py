"""The model step. Produces DeckPlans; never sees XML, coordinates or file bytes.

Two implementations behind one interface:
- StubPlanner: offline and deterministic, so the POC runs here with no model access.
- OpenAICompatPlanner: talks to any OpenAI-compatible /v1/chat/completions endpoint
  (vLLM, an internal gateway, a hosted API). Configured explicitly - never from environment variables.
"""
from __future__ import annotations

import json
import urllib.request

TRANSLATE_SYSTEM = """You translate PowerPoint paragraphs. Each paragraph is given as tagged text:
<rN>...</rN> is a formatted run (keep its formatting by keeping the tag), <br/> is a line break,
<fN/> is a field (slide number/date) that must appear exactly once, unchanged.
Rules: translate only the text inside tags; you MAY reorder, merge (drop a tag) or repeat tags
when the target grammar needs it; never invent tag numbers; keep numbers, tickers and product names.
Return JSON: {"paragraphs":[{"ref":"...","tagged":"..."}]} with the same refs."""

SHORTEN_SYSTEM = """Shorten each tagged paragraph by about {pct}% in {lang} while keeping its meaning and its tags.
Return JSON: {{"paragraphs":[{{"ref":"...","tagged":"..."}}]}}."""

CREATE_SYSTEM = """Plan a slide deck. Output JSON {"slides":[...]}; each slide has
"message" (the one sentence the slide proves), "layout_family" (one of: title, section,
title_and_body, two_content, title_only), "blocks" ([{"role":"title|subtitle|body",
"content":{"kind":"text","text":...} or {"kind":"bullets","items":[...]}}]) and optional "notes".
Never output coordinates, sizes, colours or fonts."""


KIT_SYSTEM = """You plan a slide deck using ONLY the slide patterns listed below, and ONLY facts from the
content pack (never invent numbers). Output JSON:
{"footer": "<short deck footer>", "slides": [{"pattern": "<name>", "fields": {...}, "items": [...],
  "table": {"header": [...], "rows": [[...]]}, "notes": "<optional speaker notes>",
  "sources": ["<fact ids used>"]}]}
Use "items" only for patterns that list items, "table" only for table patterns, and
"charts": {"<slot>": {"type": "...", "dataset": "<id>", "category": "<column>", "series": ["<column>", ...],
"title": "...", "number_format": "#,##0"}} only for chart patterns - pick a dataset from the content pack;
the numbers are taken from it automatically, never write numbers into a chart yourself. Keep text short:
headings under 6 words, bullet points under 15 words. Never output positions, sizes, colours or fonts.
Patterns:
{patterns}"""


OUTLINE_SYSTEM = """You write the CONTENT of a slide deck, slide by slide, using ONLY facts from the content
pack. Do not think about layout or design. Output JSON:
{"title": "<deck title>", "footer": "<short footer>", "slides": [
  {"purpose": "<why this slide exists>", "kind": "<cover|numbers|sequence|comparison|list|table|chart|text|closing>",
   "title": "<headline, under 8 words>", "subtitle": "<optional>", "intro": "<optional sentence>",
   "points": [{"heading": "<under 5 words>", "text": "<under 20 words>", "value": "<optional short figure, e.g. 38%>",
               "fact": "<fact id>"}],
   "table": {"header": [...], "rows": [[...]]}, "chart": {"type": "...", "dataset": "<id>", "category": "<col>", "series": ["<col>"]},
   "takeaway": "<optional one line>", "sources": ["<fact ids>"]}]}
At most {max_points} points on a slide; if there are more, use more slides. A "value" must appear in the fact it
cites. First slide is the cover, last slide the closing."""

RENDER_SYSTEM = """You place the content of ONE slide into ONE of the layouts offered. Pick the layout that suits
the content best, then fill its fields with the content. You may shorten wording to fit; never add facts or
numbers. Output JSON: {"pattern": "<layout name>", "fields": {"<field>": "<text>"}, "items": [{"<field>": "<text>"}],
"table": {"header": [...], "rows": [[...]]}, "charts": {"<slot>": {...}}}. Use only the field names listed for the
layout you pick, and an item count within its min and max. Leave out what the layout doesn't have.
If "feedback" is given, your previous answer was rejected: fix exactly what it says."""


class StubPlanner:
    name = "stub (offline, deterministic)"

    FR = {
        "Q3 Results": "<r0>Résultats du troisième trimestre</r0>",
        # word order changes: the bold "12%" run moves to the front
        "Revenue grew 12% year on year": "<r1>+12 %</r1><r0> de croissance du chiffre d'affaires</r0><r2> sur un an</r2>",
        "Cost-to-income ratio improved to 58%": "<r0>Le coefficient d'exploitation s'est amélioré à 58 %</r0>",
        "Client franchise expanded across all regions": "<r0>La franchise clients s'est développée dans toutes les régions</r0>",
        "Revenue by region": "<r0>Chiffre d'affaires par région</r0>",
        "Region": "<r0>Région</r0>", "Change": "<r0>Variation</r0>",
        "Europe": "<r0>Europe</r0>", "Asia Pacific": "<r0>Asie-Pacifique</r0>",
        "€1.2bn": "<r0>1,2 Md€</r0>", "€0.6bn": "<r0>0,6 Md€</r0>",
        "Q3 2026": "<r0>T3 2026</r0>",
        "Strengths and risks": "<r0>Forces et risques</r0>",
        "Strong fee income": "<r0>Solides revenus de commissions</r0>",
        "Rate volatility": "<r0>Volatilité des taux</r0>",
        "Emphasise the fee income story before discussing risks.":
            "<r0>Insister sur les revenus de commissions avant d'aborder les risques.</r0>",
        "Outlook for the fourth quarter": "<r0>Perspectives pour le quatrième trimestre</r0>",
        "Deposit margins expected to stabilise as rate cuts slow":
            "<r0>Les marges sur dépôts devraient se stabiliser à mesure que le rythme des baisses de taux ralentit</r0>",
        "Pipeline in structured financing remains strong into year end":
            "<r0>Le portefeuille d'opérations en financements structurés reste solide jusqu'à la fin de l'exercice</r0>",
        "Cost discipline continues with technology investment protected":
            "<r0>La discipline sur les coûts se poursuit tout en préservant les investissements technologiques</r0>",
        "Capital ratios stay comfortably above regulatory requirements":
            "<r0>Les ratios de fonds propres restent confortablement supérieurs aux exigences réglementaires</r0>",
        "Client activity in Asia expected to remain the main growth driver":
            "<r0>L'activité clients en Asie devrait rester le principal moteur de croissance</r0>",
    }
    FR_SHORT = {
        "Deposit margins expected to stabilise as rate cuts slow":
            "<r0>Marges sur dépôts en voie de stabilisation</r0>",
        "Pipeline in structured financing remains strong into year end":
            "<r0>Financements structurés : portefeuille solide</r0>",
        "Cost discipline continues with technology investment protected":
            "<r0>Coûts maîtrisés, investissements tech préservés</r0>",
    }

    def translate(self, paragraphs: list[dict], lang: str) -> dict:
        out = []
        for p in paragraphs:
            if p["text"].strip() == "":
                continue
            out.append({"ref": p["ref"], "tagged": self.FR.get(p["text"].replace("\n", " "), p["tagged"])})
        return {"mode": "translate", "target_lang": lang, "paragraphs": out}

    def shorten(self, paragraphs: list[dict], lang: str, pct: int) -> dict:
        out = [{"ref": p["ref"], "tagged": self.FR_SHORT[p["text"]]} for p in paragraphs if p["text"] in self.FR_SHORT]
        return {"paragraphs": out}

    def plan_from_kit(self, brief: str, kit_description: str, content: dict) -> dict:
        raise NotImplementedError("the offline stub has no model: pass a ready-made plan instead")

    def outline(self, brief, content, max_points):
        raise NotImplementedError("the offline stub can't write content: pass --outline outline.json")

    # Offline stage 2: a plain rule-based renderer. Picks the first layout offered (they arrive sorted
    # by fit) and copies content into same-named fields. On feedback it shortens text or drops items.
    ITEM_SOURCES = {"heading": ("heading",), "label": ("heading", "label"), "value": ("value",),
                    "body": ("text",), "points": ("text",), "caption": ("text",), "text": ("text",),
                    "detail_1": ("text",)}
    FIELD_SOURCES = {"title": "title", "subtitle": "subtitle", "intro": "intro", "takeaway": "takeaway",
                     "note": "note", "heading": "heading"}

    def render_slide(self, slide, options, context, feedback=None, previous=None):
        if previous is not None and feedback:
            return self._fix(previous, feedback)
        o = options[0]
        out = {"pattern": o["pattern"], "fields": {}}
        for f in o["fields"]:
            src = self.FIELD_SOURCES.get(f)
            if src and slide.get(src):
                out["fields"][f] = slide[src]
        pts = slide.get("points") or []
        if "items" in o:
            out["items"] = []
            for p in pts:
                item = {}
                for f in o["items"]["each"]:
                    for src in self.ITEM_SOURCES.get(f, ()):
                        if p.get(src) and not (f == "label" and src == "label" and "label" in item):
                            item[f] = p[src]
                            break
                out["items"].append(item)
        else:                                     # text layout: points go into the long-text fields
            longs = [f for f in o["fields"] if f.split("_")[0] in ("body", "points", "text", "side")]
            for f, p in zip(longs, pts):
                out["fields"][f] = (p.get("heading", "") + ": " if p.get("heading") else "") + p.get("text", "")
        if slide.get("table") and "table" in o:
            out["table"] = slide["table"]
        if slide.get("chart") and o.get("charts"):
            out["charts"] = {o["charts"][0]: slide["chart"]}
        return out

    @staticmethod
    def _fix(previous, feedback):
        import re
        fixed = json.loads(json.dumps(previous))
        for msg in feedback:
            m = re.search(r"needs (\d+)-(\d+) items, got (\d+)", msg)
            if m and fixed.get("items"):
                fixed["items"] = fixed["items"][:int(m.group(2))]
            m = re.search(r"unknown fields \[(.*?)\]", msg)
            if m:
                for f in re.findall(r"'([^']+)'", m.group(1)):
                    fixed.get("fields", {}).pop(f, None)
                    for it in fixed.get("items", []):
                        it.pop(f, None)
            m = re.search(r'too long for its box: "(.*)"; rewrite it in at most (\d+) characters', msg)
            if m:
                old, limit = m.group(1), int(m.group(2))
                def shorten(v):                   # offline: keep whole words up to the limit
                    if isinstance(v, str) and v.startswith(old[:40]) and len(v) > limit:
                        out = ""
                        for w in v.split():
                            if len(out) + len(w) + 1 > limit:
                                break
                            out = f"{out} {w}".strip()
                        return out or v[:limit]
                    return v
                fixed["fields"] = {k: shorten(v) for k, v in fixed.get("fields", {}).items()}
                fixed["items"] = [{k: shorten(v) for k, v in it.items()} for it in fixed.get("items", [])]
        return fixed

    def plan_create(self, brief: str) -> dict:
        return {"mode": "create", "slides": [
            {"message": "Title", "layout_family": "title", "blocks": [
                {"role": "title", "content": {"kind": "text", "text": "Passive mandates: fee compression"}},
                {"role": "subtitle", "content": {"kind": "text", "text": "Asset Management strategy review · Nov 2026"}}]},
            {"message": "Fee compression is concentrated in passive mandates", "layout_family": "title_and_body",
             "blocks": [
                 {"role": "title", "content": {"kind": "text", "text": "Fee compression is concentrated in passive mandates"}},
                 {"role": "body", "content": {"kind": "bullets", "items": [
                     "Passive fees down 9 bps over three years", "Active fees broadly stable",
                     "Mix shift explains two thirds of revenue decline"]}}],
             "notes": "Source: internal fee database, ev-12."},
            {"message": "Options compared", "layout_family": "two_content", "blocks": [
                {"role": "title", "content": {"kind": "text", "text": "Two responses"}},
                {"role": "body", "content": {"kind": "bullets", "items": ["Scale: consolidate index range", "Lower unit cost"]}},
                {"role": "body", "content": {"kind": "bullets", "items": ["Differentiate: thematic ETFs", "Higher margin, higher risk"]}}]},
            {"message": "Next steps", "layout_family": "section", "blocks": [
                {"role": "title", "content": {"kind": "text", "text": "Decision needed by Q1"}},
                {"role": "body", "content": {"kind": "text", "text": "Steering committee, 15 January"}}]},
        ]}


class OpenAICompatPlanner:
    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = f"openai-compatible ({self.model})"
        self.send_temperature = True

    def _chat(self, system, user) -> dict:
        return self._post([{"role": "system", "content": system}, {"role": "user", "content": user}])

    def _post(self, messages) -> dict:
        """One chat call in JSON mode. If the endpoint refuses a setting (some models reject
        temperature=0), it is dropped once and the call retried; the refusal is remembered."""
        import urllib.error
        body = {"model": self.model, "response_format": {"type": "json_object"}, "messages": messages}
        if self.send_temperature:
            body["temperature"] = 0
        req = urllib.request.Request(f"{self.base_url}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                reply = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:600]
            if e.code == 400 and "temperature" in detail and self.send_temperature:
                self.send_temperature = False
                return self._post(messages)
            raise RuntimeError(f"model endpoint answered {e.code}: {detail}") from None
        text = reply["choices"][0]["message"]["content"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:                    # tolerate a ```json fence around the object
            return json.loads(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))

    def translate(self, paragraphs, lang):
        items = [{"ref": p["ref"], "tagged": p["tagged"]} for p in paragraphs if p["text"].strip()]
        res = self._chat(TRANSLATE_SYSTEM, json.dumps({"target_language": lang, "paragraphs": items}, ensure_ascii=False))
        return {"mode": "translate", "target_lang": lang, "paragraphs": res["paragraphs"]}

    def shorten(self, paragraphs, lang, pct):
        items = [{"ref": p["ref"], "tagged": p["tagged"]} for p in paragraphs]
        return self._chat(SHORTEN_SYSTEM.format(pct=pct, lang=lang), json.dumps({"paragraphs": items}, ensure_ascii=False))

    def plan_create(self, brief):
        return {"mode": "create", **self._chat(CREATE_SYSTEM, brief)}

    def plan_from_kit(self, brief, kit_description, content):
        user = json.dumps({"request": brief, "content_pack": content}, ensure_ascii=False)
        return self._chat(KIT_SYSTEM.replace("{patterns}", kit_description), user)

    # ---- two-stage generation (controlplane/slide_flow.py) --------------------------------
    def outline(self, brief, content, max_points):
        user = json.dumps({"request": brief, "content_pack": content}, ensure_ascii=False)
        return self._chat(OUTLINE_SYSTEM.replace("{max_points}", str(max_points)), user)

    def render_slide(self, slide, options, context, feedback=None, previous=None):
        msg = {"deck": context, "slide_content": slide, "layouts": options}
        if feedback:
            msg["previous_answer"], msg["feedback"] = previous, feedback
        return self._chat(RENDER_SYSTEM, json.dumps(msg, ensure_ascii=False))

    # ---- vision: called from the CONTROL PLANE with PNGs the sandbox rendered ------------
    # The sandbox never talks to a model. These methods only ever receive picture bytes and JSON.

    vision = True

    def _chat_images(self, system, text, images: list[bytes]) -> dict:
        import base64
        parts = [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b).decode()}}
            for b in images]
        return self._post([{"role": "system", "content": system}, {"role": "user", "content": parts}])

    def label_slide(self, marked_png: bytes, boxes: list[dict], rules_kind: str, slide=None) -> dict:
        """The picture shows each box outlined with its id. -> {"kind", "use_for", "offer", "boxes": {id: role}}"""
        from .kit_merge import KINDS, ROLES
        return self._chat_images(
            "You look at one slide from a corporate template. Every box a program can fill is outlined and numbered "
            "(red = the first of a repeated item; the others repeat it). You are also given what a rule-based "
            "program guessed for each box. Say what each box is FOR in this design, judging by how it looks, not "
            "by its current words.\n"
            f"Roles: {sorted(ROLES)}. 'fixed' = decoration or a label that must stay as it is.\n"
            f"Slide kinds: {sorted(KINDS)}.\n"
            'Return JSON {"kind": "<kind>", "use_for": "<when to use this slide, max 20 words>", '
            '"offer": <false if too specific to reuse>, "boxes": {"<id>": "<role>"}}. '
            "Include only boxes from the list; omit a box if unsure.",
            json.dumps({"rules_kind": rules_kind, "boxes": boxes}, ensure_ascii=False), [marked_png])

    def compare_slides(self, example_png: bytes, test_png: bytes) -> dict:
        """-> {"same_design": true|false, "problems": ["..."]}"""
        return self._chat_images(
            "Image 1 is an example slide from a template; image 2 was built from it with placeholder text. "
            'Return JSON {"same_design": <true if image 2 keeps the design of image 1>, "problems": [<overlaps, cut-off '
            "text, broken layout; empty if none>]}. Placeholder words are expected; judge layout only.", "", [example_png, test_png])

    @classmethod
    def from_file(cls, path: str) -> "OpenAICompatPlanner":
        """JSON file: {"base_url": "...", "model": "...", "api_key": "..."}. Keep it out of git."""
        cfg = json.load(open(path))
        return cls(cfg["base_url"], cfg["model"], cfg.get("api_key", ""))


class RecordedVision(StubPlanner):
    """Replays slide labels saved earlier (a vision model's answers, or ones written for a test).

    File: {"<slide number>": {"kind": ..., "use_for": ..., "offer": ..., "boxes": {"<id>": "<role>"}}}
    It labels slides only; it doesn't judge test slides (compare_slides), so that check is skipped.
    """
    vision = True
    can_compare = False

    def __init__(self, path_or_dict):
        self.labels = path_or_dict if isinstance(path_or_dict, dict) else json.load(open(path_or_dict))

    def label_slide(self, marked_png, boxes, rules_kind, slide=None):
        return self.labels.get(str(slide), {})
