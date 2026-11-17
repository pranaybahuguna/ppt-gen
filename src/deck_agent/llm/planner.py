"""The model layer: one object, every model call.

It subclasses the engine's OpenAI-compatible client, so JSON mode, ```json fence tolerance, the
retry-without-temperature quirk and pictures for vision calls are inherited, not reimplemented.

The calls, by route:
  every turn   route (only when the rules are unsure)
  create       gather, storyline, slide_spec
  edit         split_edit, redesign_slide, tweak_slide, draw_slide
  both         review_changes (the changed or new slides, as pictures)
  translate    translate and shorten (the engine's own), review_slide (look)

Three planners:

* `AgentPlanner`    - the real one, over HTTP.
* `StubAgentPlanner`- offline and deterministic: rule-based routing and simple, working answers
                      for every call, so the whole graph runs in tests with no model at all.
* `LangChainPlanner`- the same prompts sent through `langchain-openai`, for tracing and
                      provider switching. Opt in with `llm_client=langchain`.
"""
from __future__ import annotations

import json
import re

from ..engine import bootstrap
from ..settings import Settings, settings
from .prompts import GATHER_SYSTEM, REVIEW_SYSTEM, ROUTE_SYSTEM

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

    def storyline(self, payload: dict) -> dict:
        """Facts in, the deck's argument out: action titles, a visual and cited facts per slide.

        Called by: design.plan.storyline.
        """
        from ..design.prompts import STORYLINE_SYSTEM

        return self._chat(STORYLINE_SYSTEM, json.dumps(payload, ensure_ascii=False))

    def slide_spec(self, payload: dict) -> dict:
        """One slide's plan in, its spec out (words and pieces, no coordinates).

        Called by: design.plan.write_spec, once per content slide (in parallel), and again with
        `feedback` when the spec failed a check.
        """
        from ..design.prompts import SPEC_SYSTEM
        from ..design.spec import catalog

        return self._chat(SPEC_SYSTEM.format(catalog=catalog()), json.dumps(payload, ensure_ascii=False))

    def _maybe_images(self, system: str, payload: dict, images: list[bytes]) -> dict:
        """A JSON call, with pictures when there are any and the model can see."""
        text = json.dumps(payload, ensure_ascii=False)
        if images and getattr(self, "vision", True):
            return self._chat_images(system, text, images)
        return self._chat(system, text)

    def split_edit(self, payload: dict) -> dict:
        """A request about a deck -> independent per-slide tasks. Called by: revise.nodes.split."""
        from ..revise.prompts import SPLIT_SYSTEM

        return self._chat(SPLIT_SYSTEM, json.dumps(payload, ensure_ascii=False))

    def redesign_slide(self, payload: dict, png: bytes | None = None) -> dict:
        """One slide's content + picture + request -> a new spec. Called by: revise.work.redesign."""
        from ..design.spec import catalog
        from ..revise.prompts import REDESIGN_SYSTEM

        return self._maybe_images(REDESIGN_SYSTEM.format(catalog=catalog()), payload, [png] if png else [])

    def tweak_slide(self, payload: dict, png: bytes | None = None) -> dict:
        """One slide's shapes + picture + request -> python-pptx code. Called by: revise.work.tweak."""
        from ..revise.prompts import TWEAK_SYSTEM

        return self._maybe_images(TWEAK_SYSTEM.format(), payload, [png] if png else [])

    def draw_slide(self, payload: dict, png: bytes | None = None) -> dict:
        """One slide's content + shapes + picture -> code that redraws its content area.
        Called by: revise.work.tweak in draw mode."""
        from ..revise.prompts import DRAW_SYSTEM

        return self._maybe_images(DRAW_SYSTEM.format(), payload, [png] if png else [])

    def review_changes(self, payload: dict, pngs: list[bytes]) -> dict:
        """Pictures of the changed slides + what was asked -> per-slide problems.
        Called by: revise.nodes.review."""
        from ..revise.prompts import REVIEW_SYSTEM

        return self._maybe_images(REVIEW_SYSTEM, payload, pngs)

    def review_slide(self, png: bytes, context: dict | None = None) -> dict:
        """Look at a rendered slide and say what a person would notice. -> {"ok", "problems"}

        This is the eyeball check, automated. The overflow predictor answers "does the text
        fit"; it has nothing to say about a decorative panel left empty, or a heading wedged
        into a badge - both of which were real defects here, and both of which this call found
        on the first try.
        """
        return self._chat_images(REVIEW_SYSTEM, json.dumps(context or {}, ensure_ascii=False), [png])


# ---------------------------------------------------------------- offline

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

    # ---- the create route, offline: facts in groups of three, a varied visual for each -----

    VISUALS = ("points", "cards", "statement")

    def storyline(self, payload: dict) -> dict:
        """One content slide per three facts (at most six), each titled by its first fact."""
        facts = payload.get("facts") or []
        groups = [facts[i:i + 3] for i in range(0, min(len(facts), 18), 3)]
        slides = []
        for k, g in enumerate(groups):
            words = g[0]["text"].rstrip(".").split()
            title = " ".join(words[:12]) if len(words) >= 4 else f"Point {k + 1} of the story told here"
            slides.append({"type": "content", "section": "Overview", "title": title[:80],
                           "job": "state these facts", "visual": self.VISUALS[k % len(self.VISUALS)],
                           "facts": [f["id"] for f in g]})
        request = (payload.get("request") or "A deck").strip()
        return {"title": " ".join(request.split()[:8]), "subtitle": "Built offline from the facts given",
                "sections": ["Overview"], "slides": slides}

    def slide_spec(self, payload: dict) -> dict:
        """The slide's facts, shown with the visual the storyline chose."""
        plan, facts = payload.get("slide") or {}, payload.get("facts") or []
        texts = [f["text"][:130] for f in facts] or [plan.get("job") or "A point"]
        visual = plan.get("visual")
        if visual == "statement":
            rows = [{"weight": 1, "items": [{"span": 12, "piece": "statement", "text": texts[0][:200]}]}]
        elif visual == "cards" and len(texts) >= 2:
            rows = [{"weight": 1, "items": [{"span": 12, "piece": "cards", "items": [
                        {"heading": " ".join(t.split()[:3])[:30], "text": t} for t in texts]}]},
                    {"height": 0.75, "items": [{"span": 12, "piece": "callout", "text": texts[0][:100]}]}]
        else:
            while len(texts) < 2:
                texts.append(plan.get("title") or "More to follow")
            rows = [{"weight": 1, "items": [{"span": 12, "piece": "points",
                                             "items": [{"text": t} for t in texts[:5]]}]}]
        return {"type": "content", "title": (plan.get("title") or "Slide")[:90], "rows": rows,
                "notes": "Written offline from the facts."}

    # ---- the edit route, offline: enough to drive split -> work -> join -> review ----------

    def split_edit(self, payload: dict) -> dict:
        """Every slide number named in the request becomes a redesign task (slide 1 if none)."""
        n = len(payload.get("deck") or [])
        named = _slide_numbers(payload.get("request") or "")
        slides = [s for s in named if 1 <= s <= n] or ([1] if n else [])
        return {"tasks": [{"kind": "redesign", "slide": s, "instruction": payload.get("request") or ""}
                          for s in dict.fromkeys(slides)], "summary": "offline split"}

    def redesign_slide(self, payload: dict, png: bytes | None = None) -> dict:
        """The slide's own texts, shortened, as a list of points under its title."""
        slide = payload.get("slide") or {"title": (payload.get("request") or "New slide")[:60], "texts": []}
        texts = [" ".join(t["text"].split()[:12]) for t in slide.get("texts") or [] if t.get("text")][:5]
        while len(texts) < 2:
            texts.append(slide.get("title") or "More to follow")
        return {"type": "content", "title": (slide.get("title") or "Slide")[:90],
                "rows": [{"weight": 1, "items": [{"span": 12, "piece": "points",
                                                  "items": [{"text": t} for t in texts]}]}],
                "changes": "rebuilt as a shorter list of points (offline)"}

    def tweak_slide(self, payload: dict, png: bytes | None = None) -> dict:
        return {"code": "def build(prs, ctx):\n    pass\n", "explanation": "no change (offline)"}

    draw_slide = tweak_slide

    def review_changes(self, payload: dict, pngs: list) -> dict:
        return {"slides": [{"slide": s["slide"], "ok": True, "problems": []}
                           for s in payload.get("slides") or []]}

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


    Called by: StubAgentPlanner.split_edit, rules_route.
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


    Called by: cli.cmd_doctor, cli.cmd_design, graph.deck_graph.
    """
    s = s or settings()
    if s.llm_client == "stub":
        return StubAgentPlanner()
    cfg = s.llm_config()
    cls = LangChainPlanner if s.llm_client == "langchain" else AgentPlanner
    return cls(cfg["base_url"], cfg["model"], cfg.get("api_key", ""))
