"""The two tables, and how they relate: one deck has many jobs.

    decks (1) ────────< jobs (many)
       deck_id  ◀──────  deck_id

`deck_id` is also the deck's folder in the bucket and its LangGraph thread id, so the three
stores line up by name. JSON columns are JSONB on Postgres and plain JSON elsewhere (SQLite in
the unit tests).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


def now() -> datetime:
    """The current time, in UTC."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """The base of the project's tables (separate metadata from LangGraph's)."""


class Deck(Base):
    """One presentation.

    Written by: db.repo.start_job (created by a `create`), finish_job (current_file, status),
    rename_deck, delete_deck.
    """

    __tablename__ = "decks"

    deck_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    template: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="building")   # building|ready|failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    jobs: Mapped[list["Job"]] = relationship(back_populates="deck")


class Job(Base):
    """One request: an ingest, a create, an edit, a translation ...

    Written by: db.repo.start_job (queued), mark_running, finish_job (done or failed, with the
    summary, error and log), fail_stale_jobs (running when the server stopped).
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    deck_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("decks.deck_id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))      # ingest|create|edit|translate|ask|answer|attach|undo
    request: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|failed
    summary: Mapped[list] = mapped_column(JSONType, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    log: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deck: Mapped[Deck | None] = relationship(back_populates="jobs")
