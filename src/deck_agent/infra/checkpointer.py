"""The checkpointer: why a conversation survives a restart.

LangGraph writes the state after every node into a checkpoint keyed by `thread_id`. That is
what makes "now make slide 2 shorter" possible three messages later, and it is also what makes
`interrupt()` work - the graph genuinely stops, the process can exit, and the run resumes from
the stored state when the answer arrives.

Postgres rather than a file: more than one agent process will be answering turns for the same
conversation, and a checkpoint has to be visible to all of them.

`InMemorySaver` is offered for tests and one-shot CLI runs. It is not a smaller Postgres: when
the process ends, the conversation is gone, and `interrupt()` can only be resumed inside the
same process. Choose it deliberately.
"""
from __future__ import annotations

import contextlib

from ..errors import Boundary
from ..settings import Settings, settings


#: The in-memory checkpointer has to be *one* object for the whole process. A fresh saver per
#: call is not a checkpointer at all: the second turn of a conversation would find no thread,
#: and `interrupt()` would have nothing to resume. Postgres needs no equivalent, because the
#: database is the shared thing.
_in_memory = None


@contextlib.contextmanager
def checkpointer(s: Settings | None = None):
    """Yield a checkpointer. Use as a context manager: the Postgres pool closes on exit.

    Called by: cli.cmd_clean, cli.cmd_doctor, facade._tidy_if_asked, graph.deck_graph, migrate.
    """
    global _in_memory
    s = s or settings()
    if s.checkpointer == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        if _in_memory is None:
            _in_memory = InMemorySaver()
        yield _in_memory
        return
    if s.checkpointer != "postgres":
        raise Boundary(f"unknown checkpointer {s.checkpointer!r}: use postgres or memory")

    from langgraph.checkpoint.postgres import PostgresSaver

    try:
        with PostgresSaver.from_conn_string(s.pg_dsn) as saver:
            yield saver
    except Exception as e:                       # psycopg errors are not worth leaking raw
        if _is_connection_error(e):
            raise Boundary(
                f"Postgres is not reachable ({e.__class__.__name__}). Run: make up\n"
                f"  dsn: {s.redacted()['pg_dsn']}"
            ) from None
        raise


def reset_memory() -> None:
    """Tests only: forget the process-wide in-memory checkpointer.

    Called by: the test fixtures only (tests/conftest.py).
    """
    global _in_memory
    _in_memory = None


def migrate(s: Settings | None = None) -> str:
    """Create the checkpoint tables. Idempotent - LangGraph tracks its own migrations.

    Called by: cli.cmd_init.
    """
    s = s or settings()
    if s.checkpointer == "memory":
        return "memory checkpointer: nothing to migrate"
    with checkpointer(s) as saver:
        saver.setup()
    return f"checkpoint tables ready in {s.redacted()['pg_dsn']}"


def _is_connection_error(e: Exception) -> bool:
    """Whether a Postgres error means the database is not reachable (as opposed to a real error).

    Called by: checkpointer(), to turn a psycopg traceback into 'run make up'.
    """
    text = f"{e.__class__.__name__}: {e}".lower()
    return any(w in text for w in ("operationalerror", "could not connect", "connection refused",
                                   "timeout expired", "no such host", "could not translate"))
