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
    )
    settings_module.reset(s)
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
