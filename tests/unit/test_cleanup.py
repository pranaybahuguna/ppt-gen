"""Clean-up: what may go, and what a session still needs.

The risk in a clean-up is not that it deletes too little. It is that it deletes a file the
conversation will ask for on the next turn - the plan an edit rebuilds from, the version undo
goes back to - and the deck breaks a week later, far from the cause. So these tests build a
real session (create, then edit) and check that after a tidy the session still works.
"""
from __future__ import annotations

import pytest

from deck_agent import facade
from deck_agent.infra import cleanup
from deck_agent.infra.checkpointer import checkpointer
from deck_agent.infra.storage import Ref, sha256_file, store

from .test_graph import CONTENT, OUTLINE

QUIET = {"log": lambda *a, **k: None}


@pytest.fixture
def two_turns(offline_settings, template, planner):
    """A deck created and then edited: two runs, two versions."""
    tpl = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
    created = facade.create_deck("keep-me", "pilot", CONTENT, tpl, planner=planner,
                                 outline=OUTLINE, **QUIET)
    assert created["ok"], created
    edited = facade.edit_deck("keep-me", "make slide 2 less wordy", planner=planner, **QUIET)
    assert edited["ok"], edited
    return created, edited


def keys(prefix=""):
    return {Ref.parse(u).key for u in store().list(prefix)}


def test_tidy_keeps_what_the_session_needs(two_turns):
    created, edited = two_turns
    with checkpointer() as saver:
        values = cleanup.state_of(saver, "keep-me")
        needed = cleanup.referenced(values, store().bucket)
        plan = cleanup.plan_tidy(store(), saver, "keep-me")
        going = {o.uri for o in plan.objects}

        assert not going & needed, "tidy would delete something the session refers to"
        assert created["deck"] in needed and edited["deck"] in needed, "undo needs every version"
        assert values["deck_plan"] in needed, "an edit rebuilds from this plan"
        latest = values["run_id"]
        assert not any(f"/{latest}/" in u for u in going), "the latest run is kept whole"
        assert not any(u.endswith("/manifest.json") for u in going), "manifests are the history"
        assert going, "an earlier run's previews and ops should have been dropped"

        cleanup.execute(store(), saver, plan)

    # and the session still works afterwards: undo, then another edit
    assert facade.undo("keep-me")["ok"]
    again = facade.edit_deck("keep-me", "make slide 1 less wordy", **QUIET)
    assert again["ok"], again


def test_purge_removes_files_and_session_together(two_turns):
    with checkpointer() as saver:
        plan = cleanup.plan_purge(store(), saver, ["keep-*"])
        assert plan.threads == ["keep-me"]
        cleanup.execute(store(), saver, plan)
        assert "keep-me" not in cleanup.thread_ids(saver)
    assert keys("keep-me/") == set()
    assert keys("uploads/"), "the template is shared and is not part of a deck"
    assert facade.session("keep-me")["exists"] is False


def test_shared_prefixes_are_never_purged_as_decks(two_turns):
    with checkpointer() as saver:
        plan = cleanup.plan_purge(store(), saver, ["*"])
    assert not any(o.key.startswith(("uploads/", "kits/")) for o in plan.objects)
    assert any("shared" in n for n in plan.notes)


def test_orphans_are_files_without_a_session_and_sessions_without_files(two_turns):
    store().put_json({"x": 1}, "lost-deck/v001.json")
    with checkpointer() as saver:
        plan = cleanup.plan_orphans(store(), saver)
        assert {o.key for o in plan.objects} == {"lost-deck/v001.json"}

        store().delete(store().list("keep-me/"))
        plan = cleanup.plan_orphans(store(), saver)
        assert plan.threads == ["keep-me"], "a session whose files are gone is broken"


def test_everything_can_keep_templates(two_turns):
    with checkpointer() as saver:
        plan = cleanup.plan_everything(store(), saver, keep_templates=True)
        assert not any(o.key.startswith(("kits/", "uploads/")) for o in plan.objects)
        assert plan.threads == ["keep-me"]
        cleanup.execute(store(), saver, plan)
    assert keys() and all(k.startswith(("kits/", "uploads/")) for k in keys())


def test_a_dry_run_deletes_nothing(two_turns):
    before = keys()
    with checkpointer() as saver:
        cleanup.plan_everything(store(), saver)
    assert keys() == before


def test_tidy_after_turn(offline_settings, template, planner):
    object.__setattr__(offline_settings, "tidy_after_turn", True)
    tpl = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
    created = facade.create_deck("auto", "pilot", CONTENT, tpl, planner=planner, outline=OUTLINE,
                                 **QUIET)
    first_run = Ref.parse(created["manifest"]).key.split("/")[1]
    assert f"auto/{first_run}/ops.json" in keys("auto/")

    edited = facade.edit_deck("auto", "make slide 2 less wordy", planner=planner, **QUIET)
    assert edited["ok"] and "tidied" in edited, edited
    assert "error" not in edited["tidied"] and edited["tidied"]["deleted_objects"] > 0
    assert f"auto/{first_run}/ops.json" not in keys("auto/"), "the first run's bulk should be gone"
    assert f"auto/{first_run}/manifest.json" in keys("auto/")
    assert facade.edit_deck("auto", "make slide 1 less wordy", planner=planner, **QUIET)["ok"]


@pytest.mark.parametrize("text, seconds", [("30m", 1800), ("12h", 43200), ("7d", 604800),
                                           ("2w", 1209600)])
def test_ages(text, seconds):
    assert cleanup.parse_age(text) == seconds


def test_a_bad_age_says_what_it_wants():
    with pytest.raises(ValueError, match="7d"):
        cleanup.parse_age("soon")


# --------------------------------------------------------------------------- one kit only

def test_ingesting_keeps_only_the_new_kit_and_its_template(offline_settings):
    st = store()
    for key in ("kits/aaa.json", "kits/bbb.json"):
        st.put_json({"patterns": {}}, key)
    for key in ("uploads/aaa-new.pptx", "uploads/bbb-old.pptx", "uploads/ccc-attached-deck.pptx"):
        st.put_bytes(b"x", key)

    out = cleanup.keep_only_kit(st, "aaa")

    assert keys("kits/") == {"kits/aaa.json"}
    assert keys("uploads/") == {"uploads/aaa-new.pptx", "uploads/ccc-attached-deck.pptx"}, \
        "the old kit's template goes with it; an attached deck with no kit is not touched"
    assert len(out["removed_kits"]) == 1 and len(out["removed_templates"]) == 1


def test_single_kit_keeps_an_unknown_templates_kit_with_the_deck(offline_settings, template,
                                                                planner):
    object.__setattr__(offline_settings, "single_kit", True)
    store().put_json({"patterns": {}}, "kits/the-ingested-one.json")
    tpl = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")

    created = facade.create_deck("other-tpl", "pilot", CONTENT, tpl, planner=planner,
                                 outline=OUTLINE, **QUIET)
    assert created["ok"], created
    assert keys("kits/") == {"kits/the-ingested-one.json"}, "only `deckops kit` writes kits/"
    assert any(k.startswith("other-tpl/kit-") for k in keys("other-tpl/"))
    assert facade.edit_deck("other-tpl", "make slide 2 less wordy", planner=planner, **QUIET)["ok"]

    with checkpointer() as saver:   # and purging the deck takes its kit with it
        cleanup.execute(store(), saver, cleanup.plan_purge(store(), saver, ["other-tpl"]))
    assert keys("kits/") == {"kits/the-ingested-one.json"} and not keys("other-tpl/")


def test_a_deck_whose_kit_was_replaced_says_why_it_cannot_rebuild(two_turns):
    from deck_agent.nodes.edit import _template_still_there

    with checkpointer() as saver:
        values = cleanup.state_of(saver, "keep-me")
    assert _template_still_there(values)[0]
    store().delete([values["kit"]])
    can, why = _template_still_there(values)
    assert not can and "replaced" in why


# --------------------------------------------------------------------------- keep_versions

def test_only_the_newest_versions_are_kept(offline_settings, template, planner):
    """Each version is a whole deck; with keep_versions=2 the older ones are deleted, the
    numbering carries on from the newest, and undo still goes back one step."""
    object.__setattr__(offline_settings, "keep_versions", 2)
    tpl = store().put(template, f"uploads/{sha256_file(template)}-template.pptx")
    facade.create_deck("kv", "pilot", CONTENT, tpl, planner=planner, outline=OUTLINE, **QUIET)
    assert facade.edit_deck("kv", "make slide 2 less wordy", planner=planner, **QUIET)["ok"]
    third = facade.edit_deck("kv", "make slide 1 less wordy", planner=planner, **QUIET)
    assert third["ok"], third
    assert third["version"] == 3 and third["versions_deleted"] == 1
    session = facade.session("kv", planner=planner)
    assert [Ref.parse(u).name for u in session["versions"]] == ["v002.pptx", "v003.pptx"]
    files = sorted(k for k in keys("kv/") if k.count("/") == 1)
    assert files == ["kv/v002.pptx", "kv/v003.pptx"], files

    assert facade.undo("kv", planner=planner)["deck"].endswith("v002.pptx")
    again = facade.edit_deck("kv", "make slide 1 less wordy", planner=planner, **QUIET)
    assert again["deck"].endswith("v003.pptx"), "numbered from the newest version kept"
