"""The decks and jobs tables on the real Postgres, through SQLAlchemy and asyncpg."""
from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def pg(needs_stack):
    """The configured Postgres, with the asyncpg driver; engines are per test loop."""
    from deck_agent import settings as settings_module
    from deck_agent.db import engine

    settings_module.reset(needs_stack)
    engine.reset()
    assert engine.url().startswith("postgresql+asyncpg://")
    yield needs_stack
    engine.reset()
    settings_module.reset(None)


async def test_rows_round_trip_on_postgres(pg):
    from deck_agent.db import engine, repo

    deck_id = f"test-db-{uuid.uuid4().hex[:8]}"
    try:
        job = await repo.start_job("create", deck_id, {"brief": "x"}, title="Test")
        await repo.mark_running(job)
        await repo.finish_job(job, ok=True, summary=["built"], log=["a"],
                              deck_file=f"s3://deckops/{deck_id}/v001.pptx")
        deck = await repo.get_deck(deck_id)
        assert deck.status == "ready" and deck.title == "Test"
        row = await repo.get_job(job)
        assert row.request == {"brief": "x"} and row.summary == ["built"], "JSONB round-trips"
        assert row.finished_at.tzinfo is not None, "timestamps keep their time zone"
        assert [j.job_id for j in await repo.list_jobs(deck_id)] == [job]
    finally:
        await repo.delete_decks([deck_id])
        await engine.dispose()
    assert await repo.get_deck(deck_id) is None
    await engine.dispose()
