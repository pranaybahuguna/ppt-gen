"""Test fixtures.

The split that matters: **`tests/unit` must pass with nothing running.** No containers, no
Postgres, no object storage, no model. That is achievable because the single-process design
leaves almost everything pure: probing, compiling, applying edits, the byte-preserving save
and the geometry checks are all in-process Python, so they can be tested directly.

`tests/integration` is for the parts that genuinely need the outside world - the sandbox
container, Postgres, the S3 service - and each of those tests skips itself, with a reason,
when the thing it needs is not there. A skipped integration test is informative; a unit test
that needs Docker is not.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

SAMPLES = REPO / "local" / "samples"


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs docker, Postgres or object storage")


@pytest.fixture
def anyio_backend():
    """Async tests (`@pytest.mark.anyio`) run on asyncio, the loop the server uses."""
    return "asyncio"


@pytest.fixture(scope="session")
def samples() -> Path:
    """The engine's own sample deck and example template, built if they are not there yet."""
    engine = REPO / "engine" / "deckops"
    if not (SAMPLES / "sample.pptx").exists() or not (SAMPLES / "example_style.pptx").exists():
        SAMPLES.mkdir(parents=True, exist_ok=True)
        for script in ("samples/make_sample.py", "samples/make_style_template.py"):
            subprocess.run([sys.executable, script, str(SAMPLES)], cwd=engine, check=True,
                           capture_output=True)
    return SAMPLES


@pytest.fixture
def sample_deck(samples) -> Path:
    """A 7-slide deck whose XML was re-serialised to look as though PowerPoint wrote it."""
    return samples / "sample.pptx"


@pytest.fixture
def template(samples) -> Path:
    """A brandable example template, with a hand-written kit beside it."""
    return samples / "example_style.pptx"


@pytest.fixture
def hand_kit(samples) -> dict:
    import json

    kit = json.loads((samples / "example_style_kit.json").read_text())
    kit["template"] = "example_style.pptx"
    return kit


@pytest.fixture
def offline_settings(tmp_path):
    """Settings with every outside dependency switched off. The default for unit tests."""
    from deck_agent import settings as settings_module
    from deck_agent.infra import checkpointer, storage
    from deck_agent.settings import Settings

    s = Settings(
        store_backend="memory",
        checkpointer="memory",
        render_backend="off",       # no LibreOffice, in a container or otherwise
        code_runner="off",          # no model-written code
        llm_client="stub",
        local_root=str(tmp_path),
        allow_custom_code=False,
        # the decks and jobs tables: SQLite in a file of this test's own - same models, same
        # queries, no Postgres needed
        db_url=f"sqlite+aiosqlite:///{tmp_path}/deckops.db",
    )
    settings_module.reset(s)
    from deck_agent.db import engine as db_engine

    db_engine.reset()
    storage.reset(storage.MemoryStore(s.s3_bucket))
    checkpointer.reset_memory()
    yield s
    settings_module.reset(None)
    storage.reset(None)
    checkpointer.reset_memory()


@pytest.fixture
def planner():
    from deck_agent.llm.planner import StubAgentPlanner

    return StubAgentPlanner()


# --------------------------------------------------------------------------- integration gates

def docker_available() -> tuple[bool, str]:
    from deck_agent.sandbox.docker import available
    from deck_agent.settings import Settings

    return available(Settings())


@pytest.fixture
def needs_sandbox():
    ok, why = docker_available()
    if not ok:
        pytest.skip(f"the sandbox is not available: {why}")


@pytest.fixture
def needs_stack():
    """Postgres and the S3 service, as `make up` starts them."""
    from deck_agent.settings import Settings

    s = Settings()
    try:
        from deck_agent.infra.storage import S3Store

        S3Store(s).ensure_bucket()
    except Exception as e:
        pytest.skip(f"object storage is not available ({e.__class__.__name__}). Run: make up")
    try:
        from deck_agent.infra.checkpointer import checkpointer

        with checkpointer(s):
            pass
    except Exception as e:
        pytest.skip(f"Postgres is not available ({e.__class__.__name__}). Run: make up && make init")
    return s


@pytest.fixture
def scratch_deck(needs_stack):
    """A deck id for one test, purged afterwards - files and session both.

    Tests that write to the real bucket and the real Postgres used to leave their decks behind,
    one `test-persist-<hex>` per run; after a day of work that was most of the bucket. The
    purge runs even when the test fails, and a purge that fails is reported, not raised, so it
    never hides the test's own result. `deckops clean --tests` catches anything left over from
    a run that was killed before this could run.
    """
    import uuid

    made: list[str] = []

    def new(kind: str = "test") -> str:
        deck_id = f"test-{kind}-{uuid.uuid4().hex[:8]}"
        made.append(deck_id)
        return deck_id

    yield new

    from deck_agent.infra import cleanup
    from deck_agent.infra.checkpointer import checkpointer
    from deck_agent.infra.storage import S3Store

    try:
        with checkpointer(needs_stack) as saver:
            cleanup.execute(S3Store(needs_stack), saver,
                            cleanup.plan_purge(S3Store(needs_stack), saver, made))
    except Exception as e:                       # noqa: BLE001
        print(f"could not clean up {made}: {e}")


@pytest.fixture
def scratch_keys(needs_stack):
    """Object keys a storage test writes; deleted afterwards."""
    from deck_agent.infra.storage import S3Store, Ref

    keys: list[str] = []
    yield keys
    st = S3Store(needs_stack)
    try:
        st.delete([Ref(st.bucket, k).uri for k in keys])
    except Exception as e:                       # noqa: BLE001
        print(f"could not clean up {keys}: {e}")
