"""The sandbox: is it actually isolated, and does the escape hatch keep its promises?

These need Docker and the built image, and skip themselves with a reason when they are not
there. They are the tests that justify the one container in this design - if the network is
reachable from inside it, or the root filesystem is writable, then the escape hatch is not
fenced in and the claim in `sandbox/__init__.py` is false.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from deck_agent.engine import deck
from deck_agent.sandbox.code import CONTRACT, run_builder
from deck_agent.sandbox.docker import read_result, run_container
from deck_agent.sandbox.render import render
from deck_agent.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def live(needs_sandbox, tmp_path):
    """Real containers, real rendering, with scratch space under the test's tmp dir."""
    from deck_agent import settings as settings_module

    s = Settings(local_root=str(tmp_path), render_backend="docker", code_runner="docker")
    settings_module.reset(s)
    yield s
    settings_module.reset(None)


def test_the_sandbox_has_no_network_and_a_read_only_root(live, tmp_path):
    work = tmp_path / "selftest"
    (work / "out").mkdir(parents=True)
    result = run_container(["python3", "/opt/runner/selftest.py", "out"], work,
                           timeout_s=120, memory="512m", cpus="1", pids=64, s=live)
    assert result.ok, result.stderr[-500:]
    report = read_result(work)

    assert report["network"].startswith("blocked"), "the sandbox reached the internet"
    assert report["dns"].startswith("blocked"), "the sandbox resolved a hostname"
    assert report["rootfs"].startswith("read-only"), "the image's filesystem was writable"
    assert report["write_work"] == "yes", "the workspace must be writable or nothing works"
    assert report["soffice"] and report["pdftoppm"], "the rendering tools are missing"


def test_files_come_back_owned_by_us_not_by_root(live, tmp_path):
    """Containers run as the host uid, or /work fills up with files nobody can delete."""
    import os

    work = tmp_path / "ownership"
    (work / "out").mkdir(parents=True)
    run_container(["python3", "-c", "open('/work/out/made.txt','w').write('x')"], work,
                  timeout_s=60, memory="256m", s=live)
    made = work / "out" / "made.txt"
    assert made.exists()
    assert made.stat().st_uid == os.getuid()


def test_a_runaway_loop_is_stopped(live, tmp_path):
    work = tmp_path / "runaway"
    (work / "out").mkdir(parents=True)
    result = run_container(["python3", "-c", "while True: pass"], work,
                           timeout_s=8, memory="256m", cpus="1", pids=32, s=live)
    assert not result.ok
    assert result.timed_out, f"expected a timeout, got exit {result.exit_code}"
    assert result.seconds < 30


def test_rendering_produces_pictures(live, template, tmp_path):
    result = render(template, tmp_path / "previews", [0, 1], s=live)
    assert result.ok, result.skipped
    assert result.backend == "docker"
    assert len(result.previews) == 2
    for png in result.previews:
        assert png.exists() and png.stat().st_size > 2000
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_rendering_off_says_so_rather_than_returning_nothing(template, tmp_path):
    from deck_agent import settings as settings_module

    s = Settings(local_root=str(tmp_path), render_backend="off")
    settings_module.reset(s)
    try:
        result = render(template, tmp_path / "previews", [0], s=s)
        assert not result.ok
        assert "turned off" in result.skipped
    finally:
        settings_module.reset(None)


# --------------------------------------------------------------------------- the escape hatch

GOOD_CODE = '''
from pptx.util import Emu, Pt


def build(prs, ctx):
    slide = prs.slides.add_slide(prs.slide_layouts[ctx.get("blank_layout", 6)])
    box = slide.shapes.add_textbox(Emu(914400), Emu(914400),
                                   Emu(ctx["slide_size"]["w"] - 1828800), Emu(1200000))
    frame = box.text_frame
    frame.word_wrap = True
    frame.text = ctx["content"]["title"]
    frame.paragraphs[0].runs[0].font.size = Pt(28)
'''


@pytest.fixture
def code_ctx(template):
    index = deck.probe(template)
    return {"slide_size": index["slide_size"],
            "style": {"palette": ["1F4E79"], "font": "Calibri", "text_colour": "374151",
                      "title_pt": 28, "body_pt": 14},
            "content": {"title": "A slide no layout could hold", "points": []},
            "insert_after": 0, "blank_layout": 6}


def test_model_written_code_runs_and_adds_a_slide(live, template, code_ctx):
    result = run_builder(GOOD_CODE, code_ctx, template, s=live)
    assert result.ok, result.problems
    assert result.slides_after == result.slides_before + 1
    assert result.deck and result.deck.exists()


def test_code_that_tries_to_reach_the_network_never_runs(live, template, code_ctx):
    hostile = ("import socket\n"
               "def build(prs, ctx):\n"
               "    socket.create_connection(('1.1.1.1', 443))\n")
    result = run_builder(hostile, code_ctx, template, s=live)
    assert not result.ok
    assert result.rejected_by_guard, "the static gate should have caught the import"
    assert "socket" in result.problems[0]


def test_code_that_imports_at_runtime_is_stopped_inside_the_container_too(live, template, code_ctx):
    """The static gate is not the boundary, so the harness enforces the same list at runtime."""
    sneaky = ("def build(prs, ctx):\n"
              "    name = 'so' + 'cket'\n"
              "    mod = __import__(name)\n")
    result = run_builder(sneaky, code_ctx, template, s=live)
    assert not result.ok


def test_code_that_never_finishes_is_killed(live, template, code_ctx):
    s = Settings(local_root=live.local_root, code_runner="docker", code_timeout_s=5)
    result = run_builder("def build(prs, ctx):\n    while True:\n        pass\n",
                         code_ctx, template, s=s)
    assert not result.ok
    assert "time" in (result.error or "").lower()


def test_a_slide_drawn_by_code_leaves_every_other_slide_byte_identical(live, sample_deck, code_ctx, tmp_path):
    """The engine's seventh rule, kept by the escape hatch as well."""
    import zipfile

    result = run_builder(GOOD_CODE, code_ctx, sample_deck, s=live)
    assert result.ok, result.problems
    sealed = tmp_path / "sealed.pptx"
    deck.reseal(sample_deck, result.deck, sealed)

    before, after = zipfile.ZipFile(sample_deck), zipfile.ZipFile(sealed)
    slides = [n for n in before.namelist()
              if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
    assert slides
    for name in slides:
        assert before.read(name) == after.read(name), f"{name} was rewritten by the code path"


def test_the_contract_the_model_is_given_matches_what_the_harness_provides(live, template, code_ctx):
    """A prompt that promises something the runtime does not provide is a silent trap."""
    for key in ("slide_size", "style", "content", "insert_after", "blank_layout"):
        assert f'ctx["{key}"]' in CONTRACT, f"the contract does not mention ctx[{key!r}]"

    reads_everything = (
        "def build(prs, ctx):\n"
        "    assert set(ctx) >= {'slide_size', 'style', 'content', 'insert_after', 'blank_layout'}\n"
        "    assert 'palette' in ctx['style'] and 'font' in ctx['style']\n"
        "    prs.slides.add_slide(prs.slide_layouts[ctx['blank_layout']])\n")
    result = run_builder(reads_everything, code_ctx, template, s=live)
    assert result.ok, result.problems


def test_slides_over_the_layout_budget_are_designed_in_the_sandbox(needs_sandbox, template,
                                                                    tmp_path, planner):
    """End to end, offline planner, real container: four slides that all want one layout.

    With a budget of one use per layout, the slides over it are reworked by code in place,
    each still where its content belongs.
    """
    from deck_agent import facade
    from deck_agent import settings as settings_module
    from deck_agent.infra import checkpointer, storage
    from deck_agent.infra.storage import sha256_file, store
    from tests.unit.test_graph import CONTENT, OUTLINE

    s = Settings(store_backend="memory", checkpointer="memory", render_backend="off",
                 code_runner="docker", llm_client="stub", local_root=str(tmp_path),
                 allow_custom_code=True, layout_reuse=1)
    settings_module.reset(s)
    storage.reset(storage.MemoryStore(s.s3_bucket))
    checkpointer.reset_memory()
    try:
        step = OUTLINE["slides"][1]
        outline = {**OUTLINE, "slides": [OUTLINE["slides"][0]] + [
            {**step, "title": f"Step group {n}"} for n in (1, 2, 3)]}
        tpl = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
        result = facade.create_deck("designed", "pilot", CONTENT, tpl, planner=planner,
                                    outline=outline, log=lambda *a, **k: None)
        assert result["ok"], result
        drawn = [c for c in result["changelog"] if c["change"].startswith("designed slide")]
        assert drawn, result["changelog"]
        assert all("keeps its template layout" not in w for w in result["warnings"]), \
            "every over-budget slide was reworked"

        local = store().get(result["deck"], tmp_path / "out.pptx")
        titles = [deck.probe(local)["slides"][i] for i in range(4)]
        texts = [" ".join(p["text"] for sh in sl["shapes"] for p in sh.get("paras", []))
                 for sl in titles]
        assert "Step group 1" in texts[1] and "Step group 2" in texts[2] \
            and "Step group 3" in texts[3], "each designed slide sits where its content belongs"
    finally:
        settings_module.reset(None)
        storage.reset(None)
        checkpointer.reset_memory()
