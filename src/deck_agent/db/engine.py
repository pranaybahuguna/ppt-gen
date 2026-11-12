"""The async engine and sessions for the decks and jobs tables.

An async engine belongs to the event loop it was created on, so there is one per running loop:
the server has one for its whole life; each command-line run gets one inside `run()` and
disposes of it at the end. Tables are created on first use (`ready`), so there is no separate
migration step to forget - `deckops init` creates them too.
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..settings import settings
from .models import Base

_engines: dict[int, tuple[AsyncEngine, async_sessionmaker]] = {}
_ready: set[int] = set()


def url() -> str:
    """The database URL: settings.db_url, or the checkpointer's Postgres with the asyncpg driver.
    """
    s = settings()
    if s.db_url:
        return s.db_url
    dsn = s.pg_dsn
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn[len(prefix):]
    return dsn


def _key() -> int:
    """The running event loop's identity: engines are kept per loop."""
    return id(asyncio.get_running_loop())


def engine() -> AsyncEngine:
    """This loop's engine, created on first use.

    Called by: sessions, ready, dispose.
    """
    key = _key()
    if key not in _engines:
        eng = create_async_engine(url(), pool_pre_ping=True)
        _engines[key] = (eng, async_sessionmaker(eng, expire_on_commit=False))
    return _engines[key][0]


async def ready() -> None:
    """Create the tables if they are not there yet. Once per engine; idempotent.

    Called by: sessions (on first use), cli.cmd_init.
    """
    eng = engine()
    if _key() in _ready:
        return
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _ready.add(_key())


async def sessions() -> async_sessionmaker[AsyncSession]:
    """A session factory for this loop, with the tables ready.

    Called by: every function in db.repo.
    """
    engine()
    await ready()
    return _engines[_key()][1]


async def dispose() -> None:
    """Close this loop's engine and forget it.

    Called by: run(), and the server when it stops.
    """
    key = _key()
    if key in _engines:
        eng, _ = _engines.pop(key)
        _ready.discard(key)
        await eng.dispose()


def run(coro_factory):
    """Run one async database operation from synchronous code, with its own engine.

    `coro_factory` is called inside the new loop: `run(lambda: repo.get_deck("x"))`.

    Called by: the command line, which is synchronous.
    """
    async def go():
        """Run the operation, then close the engine made for it."""
        try:
            return await coro_factory()
        finally:
            await dispose()
    return asyncio.run(go())


def reset() -> None:
    """Forget every engine without closing it. Tests only, between event loops."""
    _engines.clear()
    _ready.clear()
