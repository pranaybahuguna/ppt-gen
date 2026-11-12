"""gather - turn whatever the person brought into the facts a deck may state.

A person asking for a deck brings context, not a content pack: a few paragraphs typed into the
chat, a README, meeting notes, a Word document. Writing the deck's material from that is this
system's job. This node does the first half of it - *what may be said* - and the outline node
does the second, *how it is said, slide by slide*.

Why a separate step rather than handing the outline model the raw document:

* **Every fact is checked against the source.** The model returns each fact with the words that
  support it, and code looks for those words in the source, character for character once
  formatting and spacing are set aside. A fact whose quote is not there is dropped and listed in
  the report. The outline model is then told to use only these facts - so a number on a slide
  can be traced to a line in the document, and one the model made up has nowhere to come from.
* **Long context is handled once.** A forty-page document is split by its headings and read in
  parallel here; every later call sees a compact list of facts instead.
* **It can be looked at.** `content_pack.json` and `gather_report.json` sit in the run folder:
  what was taken, from which section, what was thrown away and why, and what the request needed
  that the context did not say.

What counts as context: the request itself (people often paste their notes straight into it),
plus any documents passed as `context_docs`. Plain text, Markdown, CSV and JSON are read as they
are; a .docx has its paragraphs read out of the file. A content pack supplied by the caller is
used as given and this node does nothing.
"""
from __future__ import annotations

import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..infra.storage import Ref, store
from ..settings import settings
from ..state import DeckState, step
from .common import workspace

TEXT_TYPES = {".md", ".markdown", ".txt", ".csv", ".json", ".rst", ""}
CHUNK_CHARS = 20_000           # one call reads at most this much context
MIN_FACTS = 3                  # fewer than this is not a deck; ask for more instead


# --------------------------------------------------------------------------- reading context

def read_document(name: str, data: bytes) -> str:
    """A document's text. Raises ValueError for a kind it cannot read, saying which.

    Called by: gather.
    """
    suffix = Path(name).suffix.lower()
    if suffix in TEXT_TYPES:
        return data.decode("utf-8", errors="replace")
    if suffix == ".docx":
        return _docx_text(data)
    raise ValueError(f"{name}: I can read text, Markdown, CSV, JSON and Word (.docx) "
                     f"documents, not {suffix or 'this kind of file'}. Paste the text instead.")


def _docx_text(data: bytes) -> str:
    """Paragraphs out of a .docx without a dependency: it is a zip with one XML part that
    matters. Headings keep a '#' so sections survive into the facts' `source`.

    Called by: read_document.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    out = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para))
        if not text.strip():
            continue
        heading = re.search(r'<w:pStyle w:val="(?:Heading|Title)(\d?)"', para)
        out.append(("#" * int(heading.group(1) or 1) + " " if heading else "") + text)
    return "\n".join(out)


def chunks(name: str, text: str, limit: int = CHUNK_CHARS) -> list[dict]:
    """Split long text at headings (or blank lines) into pieces of at most `limit` chars.

    Called by: gather.
    """
    if len(text) <= limit:
        return [{"name": name, "text": text}]
    parts = re.split(r"(?m)^(?=#{1,3} )", text)
    if len(parts) == 1:
        parts = text.split("\n\n")
    out, current = [], ""
    for part in parts:
        while len(part) > limit:                     # a single enormous section
            out.append(part[:limit])
            part = part[limit:]
        if len(current) + len(part) > limit and current:
            out.append(current)
            current = ""
        current += part if not current else "\n" + part
    if current:
        out.append(current)
    return [{"name": f"{name} (part {i + 1} of {len(out)})", "text": t}
            for i, t in enumerate(out)]


# --------------------------------------------------------------------------- checking

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKS = re.compile(r"[*_`#>|~\\]")
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "‘": "'",
                         "’": "'", "“": '"', "”": '"', " ": " "})


def norm(text: str) -> str:
    """What two pieces of text are compared as: no Markdown, no typographic variants, one
    space between words, lower case. Generous about formatting, strict about words.

    Called by: check, supported.
    """
    text = _LINK.sub(r"\1", str(text)).translate(_DASHES)
    text = _MARKS.sub(" ", text)
    return " ".join(text.split()).lower()


def supported(quote: str, source: str) -> bool:
    """Is the quote really in the source? An elided quote ("a ... b") needs each part, in order.

    Called by: check.
    """
    parts = [norm(p) for p in re.split(r"\.\.\.|…", quote or "") if norm(p)]
    if not parts or sum(len(p.split()) for p in parts) < 3:
        return False
    at = 0
    for p in parts:
        found = source.find(p, at)
        if found < 0:
            return False
        at = found + len(p)
    return True


def check(answer: dict, sources: list[dict]) -> tuple[dict, dict]:
    """Keep what the sources support. -> (content pack, report of what was dropped and why)

    Called by: gather.
    """
    whole = norm("\n".join(s["text"] for s in sources))
    facts, dropped, seen = [], [], set()
    for f in answer.get("facts") or []:
        text, quote = str(f.get("text") or "").strip(), str(f.get("quote") or "")
        if not text:
            continue
        if not supported(quote, whole):
            dropped.append({"fact": text, "quote": quote, "why": "quote not found in the source"})
            continue
        key = norm(text)
        if key in seen:
            continue
        seen.add(key)
        facts.append({"id": f"F{len(facts) + 1}", "text": text,
                      "source": str(f.get("source") or "context"), "quote": quote})

    datasets = {}
    for name, d in (answer.get("datasets") or {}).items():
        rows = d.get("rows") or []
        cols = d.get("columns") or []
        numbers = [str(v) for row in rows for v in row if isinstance(v, (int, float))]
        missing = [n for n in numbers if norm(_plain_number(n)) not in whole]
        if not cols or not rows or missing:
            dropped.append({"dataset": name, "why": f"numbers not in the source: {missing[:3]}"
                            if missing else "no columns or rows"})
            continue
        datasets[re.sub(r"\W+", "_", str(name)).strip("_") or f"data{len(datasets) + 1}"] = {
            "columns": cols, "rows": rows, "source": d.get("source", "context")}

    pack = {"facts": facts, "datasets": datasets, "gaps": list(answer.get("gaps") or [])}
    return pack, {"kept": len(facts), "datasets": len(datasets), "dropped": dropped}


def _plain_number(n: str) -> str:
    """1200.0 -> 1200: a number the model returned as a float was written as an integer.

    Called by: check.
    """
    return n[:-2] if n.endswith(".0") else n


# --------------------------------------------------------------------------- the node

def gather(state: DeckState, planner=None, log=print) -> dict:
    """Write the facts a new deck may state, from the request and any context documents.

    Graph node. Runs: on the create route, after route and before ensure_kit. Does nothing when
        the caller supplied a content pack or an outline.
    Reads: instruction, context_docs (object URIs), content_pack, outline.
    Writes: content_pack (facts whose quotes were found in the source); or route='ask' with
        pending_question when there is too little to go on; route='decline' without a model.
        Publishes content_pack.json and gather_report.json in the run folder.
    Next: after_gather -> ensure_kit | ask | decline.
    Called by: LangGraph, registered in graph.build_graph.
    """
    if (state.get("content_pack") or {}).get("facts") or state.get("outline"):
        return {"trace": [step("gather", outcome="supplied by the caller")]}

    sources: list[dict] = []
    request = (state.get("instruction") or "").strip()
    if request:
        sources.append({"name": "request", "text": request})
    for uri in state.get("context_docs") or []:
        name = Ref.parse(uri).name
        try:
            text = read_document(name, store().get_bytes(uri))
        except ValueError as e:
            return {"route": "ask", "pending_question": str(e),
                    "trace": [step("gather", outcome="unreadable", document=name)]}
        sources += chunks(name, text)

    if not hasattr(planner, "gather"):
        return {"route": "decline",
                "route_reason": "writing a deck from context needs a model; configure llm.json "
                                "or pass a content pack",
                "trace": [step("gather", outcome="no model")]}

    # One call per chunk, in parallel, the request travelling with each so every piece is read
    # for what the deck is about. Small contexts are a single call.
    # The request is read as a source of its own when it is the only context, or when it is long
    # enough to be notes pasted in beside a document rather than just "make me a deck about X".
    docs = [s for s in sources if s["name"] != "request"]
    others = docs + [s for s in sources if s["name"] == "request" and (not docs or len(request) > 300)]
    with ThreadPoolExecutor(max_workers=max(1, settings().render_workers)) as pool:
        answers = list(pool.map(lambda src: planner.gather(request, [src]) or {}, others))
    merged = {"facts": [f for a in answers for f in a.get("facts") or []],
              "datasets": {k: v for a in answers for k, v in (a.get("datasets") or {}).items()},
              "gaps": [g for a in answers for g in a.get("gaps") or []]}
    pack, report = check(merged, sources)
    log(f"  gather: {report['kept']} facts and {report['datasets']} datasets from "
        f"{len(sources)} source(s); {len(report['dropped'])} dropped as unsupported")

    with workspace(state) as ws:
        uri = ws.publish_json(pack, "content_pack.json")
        ws.publish_json({**report, "sources": [s["name"] for s in sources],
                         "gaps": pack["gaps"]}, "gather_report.json")

    if len(pack["facts"]) < MIN_FACTS:
        return {"route": "ask", "content_pack": None,
                "pending_question": (
                    "I don't have enough to build a deck from yet"
                    + (f" (I could only take {len(pack['facts'])} fact(s) from what you gave me)"
                       if pack["facts"] else "")
                    + ". Paste your notes, or attach the document the deck should be based on."),
                "trace": [step("gather", facts=len(pack["facts"]), outcome="too little")]}
    return {"content_pack": pack,
            "trace": [step("gather", facts=len(pack["facts"]), datasets=len(pack["datasets"]),
                           dropped=len(report["dropped"]), gaps=len(pack["gaps"]),
                           content_pack=uri)]}


def after_gather(state: DeckState) -> str:
    """Decision after gather: ask or decline if gather said so, otherwise ensure_kit.

    Called by: LangGraph, as the conditional edge out of gather (graph.build_graph).
    """
    chosen = state.get("route")
    if chosen in ("ask", "decline"):
        return chosen
    return "ensure_kit"
