"""The model layer: one object, every model call.

The engine already defines the shape of this: a "planner" with methods like `outline`,
`render_slide`, `translate` and `label_slide`, and two implementations - an offline stub and an
OpenAI-compatible HTTP client. Keeping that interface means the engine's own two-stage
generation and kit-building code works unchanged.

This module adds the three calls the agent layer needs (`route`, `plan_edit`,
`write_slide_code`) by *subclassing* the engine's clients, so the JSON-mode handling, the
```json fence tolerance, the retry-without-temperature quirk and the vision support are all
inherited rather than reimplemented.

Three planners:

* `AgentPlanner`    - the engine's HTTP client plus the three new calls. The real one.
* `StubAgentPlanner`- offline and deterministic: rule-based routing, a rule-based stage 2 (the
                      engine's), and a *working* hand-written python-pptx builder for the code
                      path. The entire graph, escape hatch included, runs with no model at all,
                      which is what makes the tests meaningful.
* `LangChainPlanner`- the same prompts sent through `langchain-openai`, for tracing and
                      provider switching. Opt in with `llm_client=langchain`.
"""
from __future__ import annotations

import json
import re

from ..engine import bootstrap
from ..settings import Settings, settings
from .prompts import CODE_SYSTEM, EDIT_SYSTEM, GATHER_SYSTEM, REVIEW_SYSTEM, ROUTE_SYSTEM

bootstrap()
from controlplane.planner import OpenAICompatPlanner, RecordedVision, StubPlanner  # noqa: E402,F401


# ---------------------------------------------------------------- the real client

class AgentPlanner(OpenAICompatPlanner):
    """The engine's planner, plus routing, editing and code writing."""

    def route(self, instruction: str, context: dict) -> dict:
        """Ask the model which route a request is (ROUTE_SYSTEM). -> {route, confidence, reason,
        ...}

        Called by: nodes.router.route, only when the rules are unsure.
        """
        return self._chat(ROUTE_SYSTEM, json.dumps({"request": instruction, "session": context},
                                                   ensure_ascii=False))

    def gather(self, request: str, sources: list[dict]) -> dict:
        """Context in, facts out: what the deck may say. Every quote is checked afterwards."""
        return self._chat(GATHER_SYSTEM, json.dumps(
            {"request": request, "sources": sources}, ensure_ascii=False))

    def plan_edit(self, instruction: str, slides: list[dict], context: dict | None = None) -> dict:
        """Ask the model to turn an edit request into typed actions (EDIT_SYSTEM). -> {actions,
        cannot}

        Called by: nodes.edit.edit_plan.
        """
        return self._chat(EDIT_SYSTEM, json.dumps(
            {"instruction": instruction, "deck": slides, "session": context or {}}, ensure_ascii=False))

    def write_slide_code(self, contract: str, content: dict, style: dict, ctx: dict,
                         feedback: list[str] | None = None, previous: str | None = None) -> dict:
        """Ask the model for python-pptx code that reworks or draws one slide (CODE_SYSTEM with the
        sandbox contract). With feedback, the previous code and what was wrong go with it.
        -> {code, form, explanation}

        Called by: nodes.custom_code.custom_code (first drafts in parallel, then retries).
        """
        msg = {"content": content, "style": style, "ctx_example": ctx}
        if feedback:
            msg["feedback"], msg["previous_code"] = feedback, previous
        return self._chat(CODE_SYSTEM.format(contract=contract), json.dumps(msg, ensure_ascii=False))

    def review_slide(self, png: bytes, context: dict | None = None) -> dict:
        """Look at a rendered slide and say what a person would notice. -> {"ok", "problems"}

        This is the eyeball check, automated. The overflow predictor answers "does the text
        fit"; it has nothing to say about a decorative panel left empty, or a heading wedged
        into a badge - both of which were real defects here, and both of which this call found
        on the first try.
        """
        return self._chat_images(REVIEW_SYSTEM, json.dumps(context or {}, ensure_ascii=False), [png])


# ---------------------------------------------------------------- offline

#: The stub's answer for the code path. It is real, working python-pptx: a row of panels with a
#: heading and a line of text in each, drawn from ctx["style"]. Having a *correct* canned answer
#: means the escape hatch is exercised end to end by the tests - container, reseal, checks and
#: all - with no model in the loop.
STUB_SLIDE_CODE = '''
from pptx.util import Emu, Pt
from pptx.dml.color import RGBColor


def _rgb(hexc):
    return RGBColor.from_string((hexc or "222222").lstrip("#")[:6].upper())


def build(prs, ctx):
    w, h = ctx["slide_size"]["w"], ctx["slide_size"]["h"]
    style = ctx["style"]
    content = ctx["content"]
    margin = int(w * 0.06)
    top, box_h = int(h * 0.32), int(h * 0.42)
    if ctx.get("target_slide") is not None:
        # rework in place: keep title, page and frame art; clear the old layout's content
        slide = prs.slides[ctx["target_slide"]]
        content_ids = {s["id"] for s in ctx.get("shapes") or [] if s["role"] == "content"}
        for sh in list(slide.shapes):
            if sh.shape_id in content_ids:
                sh._element.getparent().remove(sh._element)
        box = ctx["body_box"]
        margin, top, box_h = box["x"], box["y"] + int(box["h"] * 0.1), int(box["h"] * 0.7)
        w = box["x"] * 2 + box["w"]
    else:
        slide = prs.slides.add_slide(prs.slide_layouts[ctx.get("blank_layout", 6)])
        title = slide.shapes.add_textbox(Emu(margin), Emu(int(h * 0.08)),
                                         Emu(w - 2 * margin), Emu(int(h * 0.14)))
        tf = title.text_frame
        tf.word_wrap = True
        tf.text = content.get("title", "")
        run = tf.paragraphs[0].runs[0]
        run.font.size = Pt(style.get("title_pt", 28))
        run.font.bold = True
        run.font.name = style.get("font", "Calibri")
        run.font.color.rgb = _rgb(style.get("text_colour"))

    items = (content.get("points") or [])[:4]
    if not items:
        return
    gap = int(w * 0.02)
    box_w = (w - 2 * margin - gap * (len(items) - 1)) // len(items)
    palette = style.get("palette") or ["3B82F6"]
    for i, item in enumerate(items):
        x = margin + i * (box_w + gap)
        panel = slide.shapes.add_textbox(Emu(x), Emu(top), Emu(box_w), Emu(box_h))
        body = panel.text_frame
        body.word_wrap = True
        body.text = item.get("heading", "")
        p0 = body.paragraphs[0].runs[0]
        p0.font.size = Pt(style.get("body_pt", 14) + 2)
        p0.font.bold = True
        p0.font.name = style.get("font", "Calibri")
        p0.font.color.rgb = _rgb(palette[i % len(palette)])
        para = body.add_paragraph()
        r = para.add_run()
        r.text = item.get("text", "")
        r.font.size = Pt(style.get("body_pt", 14))
        r.font.name = style.get("font", "Calibri")
        r.font.color.rgb = _rgb(style.get("text_colour"))
'''

#: Words that decide a route without a model. Order matters: translate wins over edit, because
#: "translate slide 3 into French" names a slide but is not an edit.
TRANSLATE_WORDS = re.compile(r"\b(translat\w*|in (french|german|spanish|italian|dutch|portuguese|"
                             r"japanese|chinese|arabic|hindi)\b|en (fran[cç]ais|espa[nñ]ol))",
                             re.IGNORECASE)
RESTYLE_WORDS = re.compile(
    r"\b(restyle|re-?brand|re-?skin"
    r"|(?:move|put|redo|rebuild)\s+(?:it|this|that|the deck)?\s*(?:on)?(?:in)?to\b"
    r"|in(?:to)?\s+(?:the|our|a|another|this)\s+\S*\s*(?:corporate\s+)?(?:template|style|brand|look)"
    r"|apply\s+(?:the|our)\s+\S*\s*template)\b", re.IGNORECASE)
#: Words that point at *another template*, not at how one slide looks. "Restyle" means moving
#: the deck onto a different template only when one of these is present; otherwise a restyle of
#: named slides is an edit (a different layout or different colours, same template).
OTHER_TEMPLATE = re.compile(
    r"\b(templates?|brand(?:ing)?|corporate|house[\s-]style|master|onto|another deck)\b",
    re.IGNORECASE)
SLIDE_WORD = re.compile(r"\bslides?\b", re.IGNORECASE)
EDIT_WORDS = re.compile(r"\b(edit|change|fix|update|shorten|rewrite|reword|replace|remove|delete|"
                        r"add (a )?slide|drop|swap|tighten|less wordy|make it)\b", re.IGNORECASE)
LANGS = {"french": "fr", "german": "de", "spanish": "es", "italian": "it", "dutch": "nl",
         "portuguese": "pt", "japanese": "ja", "chinese": "zh", "arabic": "ar", "hindi": "hi",
         "français": "fr", "español": "es"}


class StubAgentPlanner(StubPlanner):
    """Offline. Deterministic. Good enough to drive every path in the graph."""

    name = "stub (offline, deterministic)"
    vision = False

    def route(self, instruction: str, context: dict) -> dict:
        """Offline routing: the rules alone (rules_route).

        Called by: nodes.router.route, in tests and the demo.
        """
        return rules_route(instruction, context) | {"from": "stub"}

    def gather(self, request: str, sources: list[dict]) -> dict:
        """Offline: every sentence of prose becomes a fact, quoting itself. Crude, but every
        quote is genuinely in the source, so it exercises the same checks as a model would."""
        facts = []
        for src in sources:
            section, in_code = "", False
            for line in src["text"].splitlines():
                stripped = line.strip()
                if stripped.startswith("```"):
                    in_code = not in_code
                    continue
                if in_code:
                    continue
                if stripped.startswith("#"):
                    section = stripped.lstrip("#").strip()
                    continue
                if not stripped or stripped.startswith(("```", "|", "    ")):
                    continue
                for sentence in re.split(r"(?<=[.!?])\s+", stripped.lstrip("*-> ").strip()):
                    if len(sentence) >= 25:
                        facts.append({"text": sentence, "quote": sentence,
                                      "source": f"{src['name']} > {section}".rstrip(" >")})
        return {"facts": facts[:40], "datasets": {}, "gaps": []}

    def plan_edit(self, instruction: str, slides: list[dict], context: dict | None = None) -> dict:
        """A tiny but real editor: `set_text` on the slides named, shortened if asked."""
        want = _slide_numbers(instruction) or []
        actions = []
        for s in slides:
            if want and s["slide"] not in want:
                continue
            for box in s.get("boxes", []):
                para = (box.get("paragraphs") or [None])[0]
                if not para:
                    continue
                text = para["text"]
                if "less wordy" in instruction.lower() or "shorten" in instruction.lower():
                    short = " ".join(text.split()[: max(2, len(text.split()) // 2)])
                    if short != text:
                        actions.append({"action": "set_text", "ref": para["ref"], "text": short})
                break
        return {"actions": actions, "cannot": None if actions else "the offline stub only shortens text"}

    def write_slide_code(self, contract, content, style, ctx, feedback=None, previous=None) -> dict:
        """Offline code: a fixed, working builder (STUB_SLIDE_CODE) that draws or reworks a slide
        as a
        row of panels.

        Called by: nodes.custom_code.custom_code, in tests and the demo.
        """
        return {"code": STUB_SLIDE_CODE, "explanation": "a row of panels, one per point"}

    def review_slide(self, png, context=None) -> dict:
        """The offline stub cannot see. Saying so beats quietly passing every slide."""
        return {"ok": True, "problems": [], "note": "no vision model: nothing was looked at"}


def rules_route(instruction: str, context: dict) -> dict:
    """Routing without a model. Used by the stub, and tried first by the real router.

    These are the cases where a model call would be waste: there is no deck at all, or the verb
    is unambiguous. Anything else returns a low confidence and lets the caller decide whether to
    spend a call or ask the person.

    Called by: StubAgentPlanner.route, nodes.router.route.
    """
    text = instruction or ""
    has_deck = bool(context.get("has_current_deck") or context.get("has_attached_deck"))
    slides = _slide_numbers(text)

    # With no deck in the session there is nothing to edit, translate or restyle, whatever the
    # words say. "a new deck in this style" is a create, not a restyle.
    if not has_deck:
        return {"route": "create", "slides": [], "target_lang": None, "confidence": 0.95,
                "reason": "there is no deck to work on, so this must be a new one"}
    if TRANSLATE_WORDS.search(text):
        lang = next((c for w, c in LANGS.items() if re.search(rf"\b{w}\b", text, re.IGNORECASE)), None)
        m = re.search(r"\b(?:to|into|in)\s+([a-z]{2})\b", text, re.IGNORECASE)
        return {"route": "translate", "slides": slides, "target_lang": lang or (m.group(1).lower() if m else None),
                "confidence": 0.9 if (lang or m) else 0.5,
                "reason": "the request asks for another language"}
    if RESTYLE_WORDS.search(text):
        # "Restyle slide 3" is not "move the deck onto another template". It asks for one
        # slide to look different inside the template it already uses, which the edit route
        # can do (change_layout, set_style). This used to be declined with an answer about
        # moving the whole deck, and the slide number was dropped on the way.
        if SLIDE_WORD.search(text) and not OTHER_TEMPLATE.search(text):
            return {"route": "edit", "slides": slides, "target_lang": None,
                    "confidence": 0.85 if slides else 0.5,
                    "reason": "restyling a particular slide is an edit: a different layout or "
                              "different colours, in the template the deck already uses"}
        return {"route": "restyle", "slides": slides, "target_lang": None, "confidence": 0.7,
                "reason": "the request asks for the deck in a different template's look"}
    if EDIT_WORDS.search(text) or slides:
        return {"route": "edit", "slides": slides, "target_lang": None,
                "confidence": 0.85 if EDIT_WORDS.search(text) else 0.6,
                "reason": "the request changes the deck that is already here"}
    return {"route": "ask", "slides": [], "target_lang": None, "confidence": 0.3,
            "reason": "there is a deck here but the request does not say what to do with it",
            "question": "Do you want me to change this deck, translate it, or start a new one?"}


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
             "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}


def _slide_numbers(text: str) -> list[int]:
    """'slide 3', 'slides 2 and 5', 'slides 2-4', 'the first slide' -> [3] / [2,5] / [2,3,4] / [1].


    Called by: StubAgentPlanner.plan_edit, rules_route.
    """
    out: list[int] = []
    for a, b in re.findall(r"slides?\s+(\d+)\s*(?:-|to|through)\s*(\d+)", text, re.IGNORECASE):
        out += list(range(int(a), int(b) + 1))
    if not out:
        m = re.search(r"slides?\s+([\d,\s]+(?:and\s*\d+)?)", text, re.IGNORECASE)
        if m:
            out += [int(n) for n in re.findall(r"\d+", m.group(1))]
    for word, n in _ORDINALS.items():
        if re.search(rf"\b{word}\s+slide\b", text, re.IGNORECASE):
            out.append(n)
    return sorted({n for n in out if n != 0})


# ---------------------------------------------------------------- langchain variant

class LangChainPlanner(AgentPlanner):
    """The same prompts, sent through `langchain-openai`.

    Why offer it: tracing, retries and provider switching come for free, and `LangGraph` is from
    the same family. Why it is not the default: the engine's own client is 40 lines, already
    handles the awkward parts (JSON mode, fenced replies, endpoints that reject `temperature`),
    and has no dependency to keep in step. Switch with `llm_client=langchain`.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", **kw):
        """The same planner, its HTTP calls made through langchain-openai's ChatOpenAI.

        Called by: planner(), when llm_client is 'langchain'.
        """
        super().__init__(base_url, model, api_key)
        from langchain_openai import ChatOpenAI

        self.name = f"langchain ({model})"
        self._llm = ChatOpenAI(model=model, api_key=api_key or "unused", base_url=f"{base_url}/v1",
                               temperature=0, timeout=180,
                               model_kwargs={"response_format": {"type": "json_object"}}, **kw)

    def _post(self, messages) -> dict:
        """Send one chat request through LangChain and return the parsed JSON answer.

        Called by: the inherited _chat and _chat_images, in place of the raw HTTP call.
        """
        reply = self._llm.invoke([(m["role"], m["content"]) for m in messages])
        text = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return json.loads(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))


# ---------------------------------------------------------------- factory

def planner(s: Settings | None = None):
    """The planner this process should use, from settings. Never reads a key from the environment.


    Called by: cli.cmd_doctor, cli.cmd_kit, graph.deck_graph; tests/validate_real_model.py.
    """
    s = s or settings()
    if s.llm_client == "stub":
        return StubAgentPlanner()
    cfg = s.llm_config()
    cls = LangChainPlanner if s.llm_client == "langchain" else AgentPlanner
    return cls(cfg["base_url"], cfg["model"], cfg.get("api_key", ""))
