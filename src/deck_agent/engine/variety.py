"""Stop a deck from using the same layout over and over.

Stage 2 places one slide at a time: it is given that slide's content and the layouts that can
hold it, and nothing else. That is deliberate and it is why long decks stay sharp - but it
means no call knows what any other call chose, and the calls run in parallel, so there is not
even a "so far" to pass. The result converges: on one real deck the model picked `panels` for
four slides out of six, and the first thing a reader said was "this looks repetitive".

Fixing it inside stage 2 would mean running the slides in sequence and feeding each one the
choices already made - slower, and it biases early slides over late ones for no good reason.
So this is a pass *after* stage 2: look at the whole deck, decide which slides are the
repetitive ones, and ask for those again with the over-used layout taken off the menu.

Two rules, because they catch different things:

* **a run** - three or more slides in a row with the same layout. This is what a reader
  actually notices, and noticing it is immediate.
* **a share** - one layout on more than half the deck. Spread out, it is less obvious, but it
  is what makes a deck feel monotonous by the end.

Neither rule touches `cover` or `closing`: there is one of each, in a fixed position, and they
are not what repetition means.

The pass is deliberately conservative. It names the fewest slides that break the rules, it
never asks for more than a third of the deck to be redone, and if a slide has no other layout
that fits, it is left exactly as it was. A repetitive deck is a small problem; a deck whose
content was rearranged to satisfy a layout rule is a much larger one.
"""
from __future__ import annotations

import math

#: Layouts that appear once, in a fixed place. Repetition is not a thing they can do.
FIXED = ("cover", "closing")

MAX_RUN = 2          # three the same in a row is the thing people see
MIN_ALLOWED = 2      # a layout used twice is never "overused", however short the deck
MAX_SHARE = 0.5      # more than half the deck being one layout is monotonous
MAX_REDONE = 1 / 3   # never ask for more than this much of the deck to be laid out again


def _kind(pattern: str | None) -> str:
    return str(pattern or "").split("@")[0]


def repetitive(patterns: list[str]) -> list[tuple[int, str]]:
    """Which slides to lay out again. -> [(index, the layout to avoid), ...]

    `patterns` is the layout chosen for each slide, in deck order. The returned indices are
    into that same list.
    """
    movable = [i for i, p in enumerate(patterns) if _kind(p) not in FIXED]
    if len(movable) < 3:
        return []

    flagged: dict[int, str] = {}

    # --- runs: in a stretch of the same layout, every slide after the second one ---------
    run_start = 0
    for i in range(1, len(patterns) + 1):
        same = i < len(patterns) and _kind(patterns[i]) == _kind(patterns[run_start])
        if same:
            continue
        run = [j for j in range(run_start, i) if j in movable]
        for j in run[MAX_RUN:]:
            flagged[j] = patterns[j]
        run_start = i

    # --- share: one layout on more than half the movable slides -------------------------
    counts: dict[str, list[int]] = {}
    for i in movable:
        counts.setdefault(_kind(patterns[i]), []).append(i)
    allowed = max(MIN_ALLOWED, math.floor(len(movable) * MAX_SHARE))
    for kind, where in counts.items():
        for j in where[allowed:]:          # keep the earliest, move the extras
            flagged.setdefault(j, patterns[j])

    # --- never redo more of the deck than is worth it ------------------------------------
    budget = max(1, math.floor(len(patterns) * MAX_REDONE))
    chosen = sorted(flagged)[:budget]
    return [(i, flagged[i]) for i in chosen]


def without(options: list[dict], avoid: str) -> list[dict]:
    """The same options with one layout removed. Empty means "leave the slide alone"."""
    return [o for o in options if _kind(o.get("pattern")) != _kind(avoid)]


def describe(changes: list[dict]) -> str:
    return ", ".join(f"slide {c['slide']} {c['from']} -> {c['to']}" for c in changes)


# --------------------------------------------------------------------------- before stage 2

def spread(jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Reorder each slide's menu, before stage 2 runs, so the deck does not converge.

    This is the cheaper half of the answer. `repetitive` above fixes a repetitive deck *after*
    the fact, which costs a model call per slide it moves; this stops most of it happening at
    all, and costs nothing.

    It **removes** the over-subscribed layout from a slide's menu rather than merely demoting
    it, and that is not the first thing it did. Reordering was tried first, on the reasoning
    that it left the model free to choose. Measured, it did almost nothing: the menus were
    reordered to put `columns` first on four slides and the model answered `panels` on three of
    them anyway. Stage 2 picks on what the content *is*, not on what order the list is in, so
    the only ordering it respects is the one it makes itself. A layout taken off the menu is a
    constraint; a layout moved down the menu is a hint, and this one is not taken.

    A slide never loses its last option: where removing would leave nothing, the slide keeps
    what it had and stays as it is.

    The walk is deliberately simple and deterministic: go through the slides in order, keep a
    count of what has been put first so far, and for each slide promote the best-fitting option
    that is neither over the deck's share nor about to make three in a row. `options_for` has
    already sorted each menu by how well it suits that slide, so "the best that is still free"
    is a real preference, not an arbitrary one.

    It is a prior, not a decision: the model may still pick something else, and the pass after
    stage 2 is what catches that. -> (jobs, what was reordered)
    """
    movable = [j for j in jobs
               if j.get("options") and _kind(j["options"][0].get("pattern")) not in FIXED]
    if len(movable) < 3:
        return jobs, []

    cap = max(MIN_ALLOWED, math.floor(len(movable) * MAX_SHARE))
    used: dict[str, int] = {}
    recent: list[str] = []
    changes: list[dict] = []

    for i, job in enumerate(jobs):
        options = job.get("options") or []
        if not options:
            continue
        best = _kind(options[0].get("pattern"))
        if best in FIXED:
            recent.append(best)
            continue

        def free(option) -> bool:
            kind = _kind(option.get("pattern"))
            if used.get(kind, 0) >= cap:
                return False
            return not (len(recent) >= MAX_RUN and all(r == kind for r in recent[-MAX_RUN:]))

        allowed = [o for o in options if free(o)]
        if allowed and len(allowed) < len(options):
            removed = sorted({_kind(o.get("pattern")) for o in options
                              if o not in allowed})
            changes.append({"slide": i + 1, "kept": [_kind(o.get("pattern")) for o in allowed],
                            "removed": removed})
            jobs[i] = {**job, "options": allowed}
        pick = (allowed or options)[0]
        kind = _kind(pick.get("pattern"))
        used[kind] = used.get(kind, 0) + 1
        recent.append(kind)
    return jobs, changes
