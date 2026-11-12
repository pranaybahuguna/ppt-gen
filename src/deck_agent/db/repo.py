"""Every read and write of the decks and jobs tables.

When rows are written:

    a request arrives        start_job      jobs: a row, status 'queued'
                                            decks: a row, status 'building' (a create only;
                                            other kinds create one only if it is missing)
    the work starts          mark_running   jobs.status = 'running'
    the work finishes        finish_job     jobs: status 'done' | 'failed', summary, error, log,
                                            finished_at; decks: current_file, status, updated_at
    a deck is renamed        rename_deck    decks.title
    a deck is deleted        delete_deck    the deck's jobs, then the deck
    the server starts        fail_stale_jobs  jobs left 'queued' or 'running' by a server that
                                            stopped are marked 'failed'
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update

from .engine import sessions
from .models import Deck, Job, now


def new_job_id() -> str:
    """A random job id."""
    return uuid.uuid4().hex[:12]


async def start_job(kind: str, deck_id: str | None, request: dict, *, title: str | None = None,
                    template: str | None = None) -> str:
    """Record a new request (status 'queued') and, for a deck, make sure its row exists. -> job_id

    Called by: server.Jobs.submit; cli.recorded.
    """
    job_id = new_job_id()
    async with (await sessions())() as db, db.begin():
        if deck_id:
            deck = await db.get(Deck, deck_id)
            if deck is None:
                db.add(Deck(deck_id=deck_id, title=title or deck_id, template=template,
                            status="building" if kind == "create" else "ready"))
            elif kind == "create":
                deck.status = "building"
                deck.title = title or deck.title
                deck.template = template or deck.template
        db.add(Job(job_id=job_id, deck_id=deck_id, kind=kind, request=request))
    return job_id


async def mark_running(job_id: str) -> None:
    """The work has started.

    Called by: server.Jobs._run; cli.recorded.
    """
    async with (await sessions())() as db, db.begin():
        await db.execute(update(Job).where(Job.job_id == job_id).values(status="running"))


async def finish_job(job_id: str, *, ok: bool, summary: list | None = None, error: str | None = None,
                     log: list | None = None, deck_file: str | None = None) -> None:
    """The work is over: record the outcome, and the deck's latest file and status.

    A failed create leaves the deck 'failed'; a failed edit leaves it as it was ('ready' - the
    previous version is still there).

    Called by: server.Jobs._run; cli.recorded.
    """
    async with (await sessions())() as db, db.begin():
        job = await db.get(Job, job_id)
        if job is None:
            return
        job.status = "done" if ok else "failed"
        job.summary, job.error, job.log, job.finished_at = summary or [], error, log or [], now()
        if job.deck_id:
            deck = await db.get(Deck, job.deck_id)
            if deck is not None:
                if deck_file:
                    deck.current_file = deck_file
                if ok or deck.status == "building":
                    deck.status = "ready" if (ok or deck.current_file) else "failed"
                deck.updated_at = now()


async def get_job(job_id: str) -> Job | None:
    """One job, or None."""
    async with (await sessions())() as db:
        return await db.get(Job, job_id)


async def list_jobs(deck_id: str | None = None, limit: int = 50) -> list[Job]:
    """Jobs, newest first; only one deck's when deck_id is given."""
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if deck_id:
        q = q.where(Job.deck_id == deck_id)
    async with (await sessions())() as db:
        return list((await db.scalars(q)).all())


async def running_job(deck_id: str) -> Job | None:
    """The deck's job that is queued or running, if there is one.

    Called by: server.Jobs.submit, to refuse a second job on the same deck.
    """
    q = select(Job).where(Job.deck_id == deck_id, Job.status.in_(("queued", "running")))
    async with (await sessions())() as db:
        return (await db.scalars(q)).first()


async def get_deck(deck_id: str) -> Deck | None:
    """One deck, or None."""
    async with (await sessions())() as db:
        return await db.get(Deck, deck_id)


async def list_decks() -> list[tuple[Deck, int]]:
    """Every deck with how many jobs it has had, most recently changed first."""
    q = (select(Deck, func.count(Job.job_id)).outerjoin(Job, Job.deck_id == Deck.deck_id)
         .group_by(Deck.deck_id).order_by(Deck.updated_at.desc()))
    async with (await sessions())() as db:
        return [(d, n) for d, n in (await db.execute(q)).all()]


async def rename_deck(deck_id: str, title: str) -> bool:
    """Set a deck's title. -> whether the deck exists."""
    async with (await sessions())() as db, db.begin():
        deck = await db.get(Deck, deck_id)
        if deck is None:
            return False
        deck.title, deck.updated_at = title, now()
        return True


async def delete_decks(deck_ids: list[str] | None) -> int:
    """Delete decks and their jobs; None means every deck and every job. -> decks deleted.

    Called by: server.op_delete; cli.cmd_clean.
    """
    async with (await sessions())() as db, db.begin():
        if deck_ids is None:
            await db.execute(delete(Job))
            return (await db.execute(delete(Deck))).rowcount or 0
        await db.execute(delete(Job).where(Job.deck_id.in_(deck_ids)))
        return (await db.execute(delete(Deck).where(Deck.deck_id.in_(deck_ids)))).rowcount or 0


async def fail_stale_jobs() -> int:
    """Mark jobs left 'queued' or 'running' as failed: the process running them has stopped.

    Called by: server.serve, when the server starts.
    """
    async with (await sessions())() as db, db.begin():
        res = await db.execute(
            update(Job).where(Job.status.in_(("queued", "running")))
            .values(status="failed", finished_at=now(),
                    error="the server stopped before this job finished"))
        return res.rowcount or 0


def job_view(job: Job) -> dict:
    """A job row as callers see it."""
    seconds = (_aware(job.finished_at or now()) - _aware(job.created_at)).total_seconds()
    return {"job_id": job.job_id, "kind": job.kind, "deck_id": job.deck_id,
            "status": job.status, "request": job.request, "summary": job.summary,
            "error": job.error, "seconds": round(seconds, 1),
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None}


def deck_view(deck: Deck, jobs: int | None = None) -> dict:
    """A deck row as callers see it."""
    out = {"deck_id": deck.deck_id, "title": deck.title, "status": deck.status,
           "template": deck.template, "current_file": deck.current_file,
           "created_at": deck.created_at.isoformat() if deck.created_at else None,
           "updated_at": deck.updated_at.isoformat() if deck.updated_at else None}
    if jobs is not None:
        out["jobs"] = jobs
    return out


def _aware(dt):
    """SQLite hands back naive datetimes; they were written in UTC."""
    from datetime import timezone

    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)
