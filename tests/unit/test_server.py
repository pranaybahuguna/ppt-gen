"""The server: REST and MCP over the same operations, offline (memory store, stub planner)."""
from __future__ import annotations

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from deck_agent import server
from deck_agent.infra.storage import sha256_file, store

from .test_graph import CONTENT, OUTLINE


@pytest.fixture
def client(offline_settings):
    server._jobs = None                       # a fresh job registry per test
    with TestClient(server.app()) as c:
        yield c
    server._jobs = None


@pytest.fixture
def template_uri(offline_settings, template):
    return store().put(template, f"uploads/{sha256_file(template)}-template.pptx")


def _create(client, template_uri, deck_id="srv", wait=True):
    return client.post(f"/decks{'?wait=true' if wait else ''}", json={
        "deck_id": deck_id, "brief": "the support bot pilot", "template": template_uri,
        "outline": OUTLINE, "content_pack": CONTENT})


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"]


def test_create_returns_a_summary_and_a_download(client, template_uri):
    r = _create(client, template_uri)
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "done", job
    result = job["result"]
    assert result["ok"] and result["versions"] == 1
    assert result["summary"][0].startswith("create: version 1")
    assert any("built" in line for line in result["summary"])
    assert result["download_url"].endswith("/decks/srv/download")

    pptx = client.get("/decks/srv/download")
    assert pptx.status_code == 200 and pptx.content[:2] == b"PK"
    assert "srv-v001.pptx" in pptx.headers["content-disposition"]


def test_an_edit_says_what_changed_and_adds_a_version(client, template_uri):
    _create(client, template_uri)
    r = client.post("/decks/srv/edit?wait=true", json={"instruction": "make slide 2 less wordy"})
    result = r.json()["result"]
    assert result["ok"] and result["versions"] == 2
    assert any(line.startswith("slide 2: text") for line in result["summary"]), result["summary"]

    session = client.get("/decks/srv").json()
    assert len(session["version_urls"]) == 2
    first = client.get(session["version_urls"][0].split("8765", 1)[-1])
    assert first.status_code == 200 and first.content[:2] == b"PK"

    assert client.post("/decks/srv/undo").json()["ok"]
    assert client.get("/decks/srv").json()["versions"] and len(client.get("/decks/srv").json()["versions"]) == 1


def test_without_wait_a_job_is_returned_and_finishes(client, template_uri):
    r = _create(client, template_uri, deck_id="srv-async", wait=False)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    for _ in range(200):
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "done", job
    assert any("assemble" in line for line in job["log"]), "the job's log shows its progress"


def test_bad_requests_say_what_is_wrong(client):
    assert client.post("/decks", json={"deck_id": "../etc", "brief": "x"}).status_code == 400
    assert client.post("/decks", json={"deck_id": "ok"}).status_code == 400
    assert client.post("/decks/ok/frobnicate", json={}).status_code == 404
    assert client.get("/decks/nothing-here/download").status_code == 404
    assert client.get("/jobs/nope").status_code == 404


@pytest.mark.anyio
async def test_one_job_per_deck_at_a_time(offline_settings):
    import threading

    server._jobs = None
    gate = threading.Event()
    first = await server.jobs().submit("x", "d", {}, lambda log: {"ok": gate.wait(5)})
    with pytest.raises(server.Busy):
        await server.jobs().submit("y", "d", {}, lambda log: {"ok": True})
    gate.set()
    assert (await server.jobs().view(first, wait=True))["status"] == "done"
    third = await server.jobs().submit("z", "d", {}, lambda log: {"ok": True})
    assert (await server.jobs().view(third, wait=True))["status"] == "done"
    server._jobs = None


def test_delete_removes_files_and_conversation(client, template_uri):
    _create(client, template_uri, deck_id="gone")
    r = client.delete("/decks/gone")
    assert r.status_code == 200 and r.json()["deleted_sessions"] == 1
    assert r.json()["deleted_rows"] == 1
    assert all(d["deck_id"] != "gone" for d in client.get("/decks").json()["decks"])
    assert client.get("/decks/gone").json()["exists"] is False


def test_the_mcp_tools_are_there_and_work(offline_settings, template_uri):
    from fastmcp import Client

    server._jobs = None

    async def go():
        async with Client(server.mcp) as c:
            names = {t.name for t in await c.list_tools()}
            assert {"create_deck", "edit_deck", "translate_deck", "ask", "answer", "undo",
                    "get_deck", "rename_deck", "list_decks", "delete_deck", "ingest_template",
                    "job_status", "list_jobs"} <= names
            missing = await c.call_tool("get_deck", {"deck_id": "nothing"})
            return missing.data
    assert asyncio.run(go())["exists"] is False
    server._jobs = None


def test_a_deck_gets_a_name_and_a_title_when_none_is_given(client, template_uri):
    r = client.post("/decks?wait=true", json={
        "brief": "The Support Bot Pilot, for ops", "template": template_uri,
        "outline": OUTLINE, "content_pack": CONTENT})
    job = r.json()
    assert job["status"] == "done", job
    deck_id = job["deck_id"]
    assert deck_id.startswith("the_support_bot_pilot_for_") and len(deck_id.rsplit("_", 1)[1]) == 6, \
        "five words of the title (here the brief), underscores, and a unique part"
    deck = client.get(f"/decks/{deck_id}").json()
    assert deck["title"] == "The Support Bot Pilot, for ops" and deck["status"] == "ready"
    assert deck["current_file"].endswith("v001.pptx")

    renamed = client.patch(f"/decks/{deck_id}", json={"title": "Pilot review"}).json()
    assert renamed["title"] == "Pilot review" and renamed["deck_id"] == deck_id
    listed = client.get("/decks").json()["decks"]
    assert [d["title"] for d in listed if d["deck_id"] == deck_id] == ["Pilot review"]


def test_jobs_are_rows_and_outlive_the_process(client, template_uri):
    """Restart the job runner (a new process would): the job is still there, from the table."""
    job_id = _create(client, template_uri, deck_id="kept").json()["job_id"]
    edit = client.post("/decks/kept/edit?wait=true", json={"instruction": "make slide 2 less wordy"})
    assert edit.json()["status"] == "done"

    server._jobs = None                           # forget everything held in memory
    after = client.get(f"/jobs/{job_id}").json()
    assert after["status"] == "done" and after["kind"] == "create"
    assert after["result"]["summary"] and after["result"]["download_url"].endswith("/decks/kept/download")

    history = client.get("/jobs?deck_id=kept").json()
    assert [j["kind"] for j in history] == ["edit", "create"]
    assert history[0]["request"] == {"instruction": "make slide 2 less wordy"}


def test_the_command_line_records_its_runs_too(offline_settings, template_uri, planner):
    import asyncio

    from deck_agent import facade
    from deck_agent.cli import recorded
    from deck_agent.db import repo

    result = recorded("create", "from-cli", {"brief": "pilot"},
                      lambda: facade.create_deck("from-cli", "pilot", CONTENT, template_uri,
                                                 planner=planner, outline=OUTLINE,
                                                 log=lambda *a, **k: None), title="Pilot")
    assert result["ok"]

    async def rows():
        return await repo.get_deck("from-cli"), await repo.list_jobs("from-cli")
    deck, jobs = asyncio.run(rows())
    assert deck.status == "ready" and deck.current_file == result["deck"]
    assert [(j.kind, j.status) for j in jobs] == [("create", "done")] and jobs[0].summary


@pytest.mark.parametrize("title, stem", [
    ("Support bot pilot - ops review!", "support_bot_pilot_ops_review"),
    ("Q3", "q3"),
    ("   ", "deck"),
    ("One two three four five six seven", "one_two_three_four_five"),
])
def test_deck_names_from_titles(title, stem):
    name = server.new_deck_id(title)
    assert name.rsplit("_", 1)[0] == stem and len(name.rsplit("_", 1)[1]) == 6
    assert server.check_deck_id(name) == name
    assert server.new_deck_id(title) != name, "two decks with one title get different names"


def test_a_title_names_the_deck_when_given(client, template_uri):
    job = client.post("/decks?wait=true", json={
        "brief": "something long about the pilot", "title": "Pilot Review for Ops",
        "template": template_uri, "outline": OUTLINE, "content_pack": CONTENT}).json()
    assert job["deck_id"].startswith("pilot_review_for_ops_"), job["deck_id"]
