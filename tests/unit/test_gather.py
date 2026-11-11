"""gather: context in, checked facts out.

The property that matters is the check. A model asked for facts will sometimes return one that
is not in the source - a rounded number, a merged claim, something it knows from elsewhere -
and the deck must not carry it. So most of these tests are about what is thrown away.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from deck_agent import facade
from deck_agent.infra.storage import sha256_file, store
from deck_agent.nodes.gather import check, chunks, gather, norm, read_document, supported
from deck_agent.state import new_state

README = """# Support bot pilot

The pilot ran for **eight weeks** across two teams.

## Results

- 38% of incoming tickets were answered by the bot.
- Median first response fell from 11 hours to 3 hours.

```bash
make demo
```

See [the report](https://example.com/report) for the full numbers.
"""
QUIET = {"log": lambda *a, **k: None}


# --------------------------------------------------------------------------- the check

def test_formatting_does_not_hide_a_real_quote():
    source = norm(README)
    assert supported("The pilot ran for eight weeks across two teams", source)
    assert supported("38% of incoming tickets were answered", source)
    assert supported("See the report for the full numbers", source), "a link's text counts"


@pytest.mark.parametrize("quote", [
    "The pilot ran for nine weeks across two teams",     # a changed number
    "40% of incoming tickets were answered by the bot",  # a rounded one
    "customers loved the bot",                           # never said
    "pilot",                                             # too short to mean anything
    "",
])
def test_a_quote_that_is_not_there_is_not_supported(quote):
    assert not supported(quote, norm(README))


def test_an_elided_quote_needs_every_part_in_order():
    source = norm(README)
    assert supported("Median first response fell ... to 3 hours", source)
    assert not supported("to 3 hours ... Median first response fell", source)


def test_check_keeps_supported_facts_and_reports_the_rest():
    answer = {"facts": [
        {"text": "The pilot lasted eight weeks", "quote": "ran for eight weeks across two teams"},
        {"text": "The bot answered 40% of tickets", "quote": "40% of incoming tickets"},
        {"text": "The pilot lasted eight weeks", "quote": "ran for eight weeks"},       # duplicate
    ], "datasets": {"made_up": {"columns": ["a", "b"], "rows": [["x", 99]]}}}
    pack, report = check(answer, [{"name": "README", "text": README}])
    assert [f["text"] for f in pack["facts"]] == ["The pilot lasted eight weeks"]
    assert pack["facts"][0]["id"] == "F1"
    assert pack["datasets"] == {}, "a dataset whose numbers are not in the source is dropped"
    assert len(report["dropped"]) == 2


def test_a_dataset_whose_numbers_are_in_the_source_is_kept():
    text = "| week | opened |\n|---|---|\n| W1 | 980 |\n| W2 | 1010 |"
    answer = {"facts": [], "datasets": {"weekly": {"columns": ["week", "opened"],
                                                   "rows": [["W1", 980], ["W2", 1010.0]]}}}
    pack, _ = check(answer, [{"name": "t", "text": text}])
    assert "weekly" in pack["datasets"]


# --------------------------------------------------------------------------- reading

def test_word_documents_are_read_with_their_headings():
    xml = ('<w:document xmlns:w="w"><w:body>'
           '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Results</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>38% of tickets were </w:t></w:r><w:r><w:t>answered.</w:t></w:r></w:p>'
           '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    assert read_document("notes.docx", buf.getvalue()) == "# Results\n38% of tickets were answered."


def test_an_unreadable_kind_says_what_it_can_read():
    with pytest.raises(ValueError, match="Paste the text"):
        read_document("scan.pdf", b"%PDF")


def test_long_context_is_split_at_headings():
    text = "\n".join(f"# Section {i}\n" + "word " * 300 for i in range(10))
    parts = chunks("big.md", text, limit=4000)
    assert len(parts) > 1
    assert all(len(p["text"]) <= 4000 for p in parts)
    assert all(p["text"].lstrip().startswith("# Section") for p in parts)


# --------------------------------------------------------------------------- the node

def test_the_node_writes_a_checked_pack_from_a_document(offline_settings, planner):
    doc = store().put_bytes(README.encode(), "uploads/abc-README.md")
    state = new_state("g", "a deck about this pilot", context_docs=[doc], run_id="r1")
    out = gather(state, planner=planner, **QUIET)
    facts = out["content_pack"]["facts"]
    assert len(facts) >= 3
    assert all(supported(f["quote"], norm(README)) for f in facts)
    assert not any("make demo" in f["text"] for f in facts), "code is not a fact"
    assert any(f["source"].startswith("abc-README.md > Results") for f in facts)
    assert store().exists(f"s3://{store().bucket}/g/r1/content_pack.json")
    assert store().exists(f"s3://{store().bucket}/g/r1/gather_report.json")


def test_too_little_context_asks_for_more(offline_settings, planner):
    out = gather(new_state("g", "a deck please", run_id="r1"), planner=planner, **QUIET)
    assert out["route"] == "ask"
    assert "attach" in out["pending_question"]


def test_made_up_facts_never_reach_the_deck(offline_settings):
    class Inventive:
        def gather(self, request, sources):
            return {"facts": [
                {"text": "Satisfaction rose to 92%", "quote": "satisfaction rose to 92%"},
                {"text": "It ran eight weeks", "quote": "ran for eight weeks across two teams"},
                {"text": "38% were answered", "quote": "38% of incoming tickets were answered"},
                {"text": "Response fell to 3h", "quote": "fell from 11 hours to 3 hours"},
            ]}

    doc = store().put_bytes(README.encode(), "uploads/abc-README.md")
    out = gather(new_state("g", "deck", context_docs=[doc], run_id="r1"), planner=Inventive(),
                 **QUIET)
    assert [f["text"] for f in out["content_pack"]["facts"]] == [
        "It ran eight weeks", "38% were answered", "Response fell to 3h"]


def test_a_supplied_content_pack_is_used_as_given(offline_settings, planner):
    pack = {"facts": [{"id": "F1", "text": "given"}], "datasets": {}}
    out = gather(new_state("g", "deck", content_pack=pack), planner=planner, **QUIET)
    assert "content_pack" not in out


def test_create_goes_through_gather_before_the_kit(offline_settings, template, planner):
    """With the stub the outline cannot be written, so the run stops there - but the trace
    shows the order, and the pack it would have written from is in the bucket."""
    tpl = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
    doc = store().put_bytes(README.encode(), "uploads/abc-README.md")
    result = facade.create_deck("from-context", "a deck about this pilot", None, tpl,
                                planner=planner, context=[doc], **QUIET)
    trace = facade.session("from-context")["trace"]
    assert trace[:4] == ["intake", "route", "gather", "ensure_kit"], trace
    assert result["ok"] is False and "model" in result["reason"]
