"""The decks and jobs tables: what is written when, offline (SQLite through aiosqlite)."""
from __future__ import annotations

import pytest

from deck_agent.db import repo

pytestmark = pytest.mark.anyio


async def test_a_job_moves_from_queued_to_done_and_updates_its_deck(offline_settings):
    job = await repo.start_job("create", "pilot", {"brief": "the pilot"}, title="The pilot",
                               template="s3://b/t.pptx")
    row = await repo.get_job(job)
    assert row.status == "queued"
    assert (await repo.get_deck("pilot")).status == "building"

    await repo.mark_running(job)
    assert (await repo.running_job("pilot")).job_id == job

    await repo.finish_job(job, ok=True, summary=["built 5 slides"], log=["gather: 9 facts"],
                          deck_file="s3://b/pilot/v001.pptx")
    row, deck = await repo.get_job(job), await repo.get_deck("pilot")
    assert row.status == "done" and row.summary == ["built 5 slides"] and row.finished_at
    assert deck.status == "ready" and deck.current_file.endswith("v001.pptx")
    assert deck.title == "The pilot" and deck.template == "s3://b/t.pptx"
    assert await repo.running_job("pilot") is None


async def test_a_failed_create_marks_the_deck_failed_and_a_failed_edit_does_not(offline_settings):
    create = await repo.start_job("create", "d", {})
    await repo.finish_job(create, ok=False, error="no layout fits")
    assert (await repo.get_deck("d")).status == "failed"

    ok = await repo.start_job("create", "e", {})
    await repo.finish_job(ok, ok=True, deck_file="s3://b/e/v001.pptx")
    edit = await repo.start_job("edit", "e", {"instruction": "x"})
    await repo.finish_job(edit, ok=False, error="nothing applicable")
    deck = await repo.get_deck("e")
    assert deck.status == "ready" and deck.current_file.endswith("v001.pptx"), \
        "a failed edit leaves the deck as it was"


async def test_one_deck_has_many_jobs_and_deleting_it_deletes_them(offline_settings):
    first = await repo.start_job("create", "p", {})
    await repo.finish_job(first, ok=True, deck_file="s3://b/p/v001.pptx")
    for kind in ("edit", "translate"):
        await repo.finish_job(await repo.start_job(kind, "p", {}), ok=True)
    decks = await repo.list_decks()
    assert [(d.deck_id, n) for d, n in decks] == [("p", 3)]
    assert [j.kind for j in await repo.list_jobs("p")][::-1] == ["create", "edit", "translate"]

    assert await repo.delete_decks(["p"]) == 1
    assert await repo.list_jobs("p") == [] and await repo.get_deck("p") is None


async def test_a_job_on_a_deck_made_elsewhere_creates_its_row(offline_settings):
    """A deck made before the tables existed (or by a host app) still gets a row on first use."""
    await repo.start_job("edit", "older-deck", {"instruction": "x"})
    deck = await repo.get_deck("older-deck")
    assert deck is not None and deck.status == "ready"


async def test_jobs_left_running_by_a_stopped_server_are_failed(offline_settings):
    left = await repo.start_job("create", "q", {})
    await repo.mark_running(left)
    assert await repo.fail_stale_jobs() == 1
    row = await repo.get_job(left)
    assert row.status == "failed" and "stopped" in row.error


async def test_rename(offline_settings):
    await repo.start_job("create", "r", {}, title="Old")
    assert await repo.rename_deck("r", "New")
    assert (await repo.get_deck("r")).title == "New"
    assert not await repo.rename_deck("nope", "x")
