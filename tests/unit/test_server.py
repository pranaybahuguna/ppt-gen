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


def test_one_job_per_deck_at_a_time(offline_settings):
    import threading

    server._jobs = None
    gate = threading.Event()
    first = server.jobs().submit("x", "d", lambda log: gate.wait(5) or {})
    with pytest.raises(server.Busy):
        server.jobs().submit("y", "d", lambda log: {})
    gate.set()
    first.future.result(5)
    assert server.jobs().submit("z", "d", lambda log: {}).future.result(5).status == "done"
    server._jobs = None


def test_delete_removes_files_and_conversation(client, template_uri):
    _create(client, template_uri, deck_id="gone")
    r = client.delete("/decks/gone")
    assert r.status_code == 200 and r.json()["deleted_sessions"] == 1
    assert client.get("/decks/gone").json()["exists"] is False


def test_the_mcp_tools_are_there_and_work(offline_settings, template_uri):
    from fastmcp import Client

    server._jobs = None

    async def go():
        async with Client(server.mcp) as c:
            names = {t.name for t in await c.list_tools()}
            assert {"create_deck", "edit_deck", "translate_deck", "ask", "answer", "undo",
                    "get_deck", "list_decks", "delete_deck", "ingest_template", "job_status"} <= names
            missing = await c.call_tool("get_deck", {"deck_id": "nothing"})
            return missing.data
    assert asyncio.run(go())["exists"] is False
    server._jobs = None
