"""Layout variety: which slides are the repetitive ones.

Stage 2 places each slide knowing nothing about the others, and in parallel, so it converges on
one layout - `panels` on four slides out of six, on a real deck. This is the pass that notices.

What is pinned here is the judgement, as a pure function over the list of chosen layouts. The
two properties that matter are opposite and equally important: it must catch a deck that really
is repetitive, and it must leave a varied deck completely alone.
"""
from __future__ import annotations

import pytest

from deck_agent.engine.variety import MAX_REDONE, describe, repetitive, spread, without


def flagged(patterns):
    return [i for i, _ in repetitive(patterns)]


# --------------------------------------------------------------------------- it catches

def test_the_deck_that_prompted_this():
    """Four slides of six were `panels`, and the first thing a reader said was "repetitive"."""
    patterns = ["cover", "steps", "panels", "panels", "panels", "panels"]
    assert flagged(patterns) == [4, 5]


def test_a_run_of_three_is_broken_up():
    patterns = ["cover", "stats", "panels", "panels", "panels", "columns", "closing"]
    assert flagged(patterns) == [4], "the third slide of the run is the one to move"


def test_the_layout_to_avoid_comes_back_with_the_slide():
    patterns = ["cover", "stats", "panels", "panels", "panels", "columns", "closing"]
    assert repetitive(patterns) == [(4, "panels")]


def test_one_layout_on_more_than_half_the_deck_is_too_much():
    """Spread out it is less obvious, but it is what makes a deck feel monotonous.

    Exactly half is allowed; more than half is not. Four of six, with no run of three, is
    caught by the share rule alone.
    """
    assert repetitive(["stats", "panels", "stats", "columns", "stats", "stats"]) != []
    assert repetitive(["stats", "panels", "stats", "columns", "stats", "panels"]) == [], \
        "exactly half is not repetition"


# --------------------------------------------------------------------------- it leaves alone

@pytest.mark.parametrize("patterns", [
    ["cover", "steps", "stats", "columns", "panels", "closing"],   # already varied
    ["cover", "panels", "panels", "stats", "closing"],             # twice is not repetition
    ["cover", "panels", "closing"],                                 # too short to judge
    ["panels", "steps", "panels", "stats", "columns", "columns"],   # mixed, nothing dominant
])
def test_a_varied_deck_is_not_touched(patterns):
    assert repetitive(patterns) == []


def test_the_cover_and_the_closing_are_never_counted():
    """There is one of each, in a fixed place. Repetition is not a thing they can do."""
    assert repetitive(["cover", "closing"]) == []
    # three `panels` among the movable slides is still caught, with the fixed ones present
    assert flagged(["cover", "panels", "panels", "panels", "closing"]) == [3]


def test_a_layout_suffix_does_not_hide_repetition():
    """The engine names a second slide of the same kind `panels@2`; it is the same layout."""
    assert flagged(["cover", "panels", "panels@2", "panels@3", "closing"]) == [3]


# --------------------------------------------------------------------------- it is bounded

def test_never_more_than_a_third_of_the_deck_is_redone():
    """A repetitive deck is a small problem. Rewriting most of it is a larger one."""
    patterns = ["panels"] * 12
    assert len(repetitive(patterns)) <= int(len(patterns) * MAX_REDONE) + 1


def test_a_whole_deck_of_one_layout_is_improved_not_rewritten():
    patterns = ["panels"] * 6
    chosen = repetitive(patterns)
    assert 0 < len(chosen) <= 2


# --------------------------------------------------------------------------- the helpers

def test_removing_a_layout_from_the_options():
    options = [{"pattern": "panels"}, {"pattern": "steps"}, {"pattern": "panels@2"}]
    assert [o["pattern"] for o in without(options, "panels")] == ["steps"]


def test_no_alternative_means_leave_the_slide_alone():
    """An empty list is the signal to keep what is there, not to fail."""
    assert without([{"pattern": "panels"}], "panels") == []


def test_the_change_reads_as_a_sentence():
    assert describe([{"slide": 4, "from": "panels", "to": "stats"}]) == "slide 4 panels -> stats"


# --------------------------------------------------------------------------- before stage 2

def menus(jobs):
    return [[o["pattern"] for o in j["options"]] for j in jobs]


def job(*patterns):
    return {"options": [{"pattern": p} for p in patterns]}


def test_the_over_subscribed_layout_is_taken_off_later_menus():
    """Measured: reordering the menu did nothing - the model answered `panels` on three slides
    whose menus had been reordered to put `columns` first. Removal is a constraint; demotion
    is a hint, and this hint is not taken."""
    jobs = [job("cover")] + [job("columns", "panels", "steps") for _ in range(5)] + [job("closing")]
    jobs, changes = spread(jobs)
    after = menus(jobs)
    assert after[1] == ["columns", "panels", "steps"], "the first slides keep their best fit"
    assert "columns" not in after[4], "a layout already used its share is removed"
    assert changes and changes[0]["removed"]


def test_a_slide_never_loses_its_last_option():
    """Where removing would leave nothing, the slide keeps what it had."""
    jobs = [job("panels") for _ in range(6)]
    jobs, _ = spread(jobs)
    assert all(j["options"] for j in jobs)
    assert menus(jobs) == [["panels"]] * 6


def test_a_deck_that_is_already_varied_is_left_alone():
    jobs = [job("cover"), job("steps"), job("stats"), job("columns"), job("panels"), job("closing")]
    jobs, changes = spread(jobs)
    assert changes == []


def test_the_cover_and_closing_do_not_use_up_a_share():
    jobs = [job("cover"), job("panels", "steps"), job("panels", "steps"),
            job("panels", "steps"), job("closing")]
    _, changes = spread(jobs)
    assert all(c["slide"] not in (1, 5) for c in changes)


def test_too_short_a_deck_is_not_second_guessed():
    jobs = [job("cover"), job("panels", "steps"), job("closing")]
    jobs, changes = spread(jobs)
    assert changes == []
