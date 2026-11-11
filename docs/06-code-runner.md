# 6. The code runner: when a model is allowed to write code

The engine's first hard rule is that the model never writes positions, sizes, colours, fonts or
XML - it writes words and picks from a menu, and plain code turns that into exact edits. This
is the one deliberate exception, and it is fenced in on four sides.

## When it is reached

One well-defined trigger: **the deterministic path produced a slide's content, but no layout in
the template can hold it**, and the relaxed fallback (`engine/fallback.py`) found nothing
either. There is nothing to fill, so rather than drop the slide or squeeze it into a layout it
does not fit, the model is asked to draw it.

It is deliberately *not* reached when text overflows after the repair rounds. That is a content
problem, and the right answer is deterministic - shorter text, then a smaller font - not
generated code. Generated code is for "this shape of content has no home", not for "this
sentence is too long".

It is off by default (`allow_custom_code`).

## Designed slides: the second trigger

A template with six layouts, used for a twelve-slide deck, repeats - and when only three of its
layouts can hold this kind of content, every attempt to vary the deck lands on one of those
three. The first README deck built here was `columns` six times and the same lightbulb layout
four times. Reordering menus (`engine/variety.py`) cannot fix that; only not using the template
for some slides can.

So with `layout_reuse` set (2 in `config/agent.json`), each template layout is used **as it
is** at most that many times per deck, and never on two slides in a row. A slide over that
budget is still built from its layout - with the template's title, header, footer and page
number, and its content in place - and then **reworked** by model-written code into a diagram:

* `render_slides` marks it, and `engine/variety.assign_forms` gives it a diagram suited to its
  content that no other reworked slide in the deck uses: process flow, timeline, staircase,
  hub and spoke, 2x2 matrix, side-by-side contrast, big numbers, layered stack, pyramid,
  cycle, icon grid, statement.
* `custom_code` gives the model that slide (`ctx["target_slide"]`) and a list of what is on
  it (`ctx["shapes"]`): the title, page number and header/footer art are marked to be left
  alone; the old layout's content is the model's to move, restyle, reuse or delete. The style
  brief gives the palette and type, and `common.design_frame` the area content may occupy.
* The slide never leaves its place in the plan, so nothing is inserted or moved afterwards,
  and repair and review always mean the slide they say.
* First drafts for every reworked slide are written in parallel; running, checking and
  accepting them is one at a time.
* Each is checked like any other slide, then - with `vision_review` on - rendered and looked
  at, and sent back with what the vision model saw.
* **If a rework fails every attempt, the slide keeps its template layout** - a repeated
  layout, never a lost slide - and the result says which.
* The template's slides are repaired *before* anything is reworked; if a rebuild happens
  later, the kept code and context rework the slides again rather than generating new code.

**Why not an empty "frame" slide to draw on.** That was tried: one emptied copy of a content
slide, kept in the template as the canvas. It removed the positioning bugs, but brought its
own - a title box that ran off a half-width header picture onto white, an empty slide the
vision review rightly called empty - and every one of them was a problem the drawing code was
not allowed to fix. Starting from a real, finished slide of the template means the title,
header and page number are exactly what that layout does on every other slide, already
checked and repaired by the normal loop.

The template stays the deck's look; it stops being the only shape a slide can take.

## The contract

The model writes a module with exactly one function. The contract lives in
`src/deck_agent/sandbox/code.py` as `CONTRACT`, next to the harness that provides it, so the
prompt and the runtime cannot drift - and there is a test that every key the contract promises
is actually in `ctx`.

```python
def build(prs, ctx):
    ...
```

| In `ctx` | What it is |
|---|---|
| `slide_size` | `{"w": EMU, "h": EMU}` |
| `style` | `palette`, `font`, `text_colour`, `title_pt`, `body_pt` - taken from the template |
| `content` | the words and numbers to place. Use these; invent nothing. |
| `insert_after` | where the slide belongs |
| `blank_layout` | the index of a layout with no placeholders |

`prs` is an open python-pptx `Presentation` of the current deck. The code adds slides; the
harness saves.

The style brief is deliberately small - enough to draw a slide that belongs with the others,
not enough to reproduce the template's layout, which is the deterministic path's job. One
detail that is not cosmetic: `title_pt` is the median of the *upper half* of the template's
font sizes, capped at 44, not the maximum. The largest size in a template is usually the cover's
60pt headline, and a 60pt title on a content slide overflows its box and the checks bounce the
slide straight back.

## Fence 1 - the static gate

`src/deck_agent/sandbox/guard.py`, before anything is shipped to a container.

**This is not the security boundary.** Anyone who wants to get past an AST filter can, and the
literature on escaping restricted Python is long and conclusive. It is there to:

* fail obviously-wrong code in milliseconds instead of spending a container on it;
* hand the model a precise, feedable reason - "you imported `requests`; this code has no
  network and may only use: ..." - which turns a dead end into a retry;
* keep the *intent* of generated code written down: the allowlist is the answer to "what is
  this code allowed to be about?".

```
allowed imports : pptx, math, copy, json, re, textwrap, itertools, functools, dataclasses,
                  typing, collections, colorsys, statistics, decimal, fractions, datetime,
                  enum, random, string, unicodedata
denied names    : eval exec compile __import__ open input globals vars getattr setattr
                  delattr breakpoint memoryview exit quit
also            : no dunder attribute access, no relative imports, a top-level build(prs, ctx),
                  no work at module level, size limits
```

## Fence 2 - the container

This is the boundary.

```
--network none              nothing in here reaches anything; DNS included
--read-only                 the image's filesystem cannot be written to
--cap-drop ALL              no capabilities
--security-opt no-new-privileges
--memory 512m --memory-swap 512m
--pids-limit 64 --cpus 1
--tmpfs /tmp:rw,size=64m
--user <host uid:gid>       files in /work come back owned by us
one container per call, removed afterwards
```

`make selftest` proves the first three from inside the container, and
`tests/integration/test_sandbox.py` asserts them.

Inside, the harness adds belt and braces - rlimits on CPU, file size, open files and processes,
and a wall-clock alarm - and replaces `__import__` with a version enforcing **the same
allowlist**, read from a file the host writes. One source of truth, so the static list and the
runtime list cannot drift.

(`__import__` cannot simply be removed: the `import` statement itself looks it up, so taking it
away stops `import pptx` from working. Replacing it is better anyway, because it also closes
the gap where a module name is assembled at runtime. There is a test for exactly that.)

Two details found by testing rather than by reasoning:

* **The harness's own alarm must beat its CPU rlimit.** If the rlimit fires first the process
  dies outright, no result file is written, and the caller sees an unexplained exit code instead
  of a sentence it can hand back to the model. The rlimit is now the backstop, with headroom.
* **Exit code 137 does not mean "out of memory".** It means SIGKILL, which both the memory
  cgroup and an outside kill produce. Telling a model it used too much memory when it actually
  ran too long sends it off fixing the wrong thing, so the container is now inspected
  (`.State.OOMKilled`) rather than guessed at.

## Fence 3 - resealing

Code written by a model goes through python-pptx, which re-serialises every part it opens. That
would quietly change parts of the deck nobody asked to change, and the engine's seventh hard
rule is that untouched slides stay byte-identical.

So the deck the sandbox produced is opened on the host and saved through the engine's
`save_preserving`, which compares every part with the original *by meaning* - canonical XML,
relationships with resolved targets, effective content types - and keeps the original bytes
wherever the meaning is unchanged.

Measured, on a real seven-slide deck:

```
preserved slide parts : slide1..slide7      all 7 byte-identical
changed               : [Content_Types].xml, ppt/presentation.xml, ppt/_rels/presentation.xml.rels
added                 : ppt/slides/slide8.xml, ppt/slides/_rels/slide8.xml.rels
```

The three changed parts are the unavoidable consequence of adding a slide. **The escape hatch
keeps the engine's hardest guarantee**, and `tests/integration/test_sandbox.py` asserts it.

One nuance: resealing matches parts by name. A deck freshly built from a kit can have
non-contiguous slide part names (`slide10.xml` with no `slide1.xml`), and python-pptx renumbers
them on save, so those parts are rewritten rather than preserved. That is harmless - the deck is
brand new, there are no untouched slides anyone cares about - but it is why the test uses a real
deck with ordinary numbering.

## Fence 4 - the same checks as everything else

The resealed deck is moved into position (a `p:sldIdLst` reorder, nothing else) and the new
slide faces the normal overflow, overlap and structural checks - **absolutely**, with no
baseline, because a slide drawn from scratch has no example slide to be compared against. That
is the right standard for it.

A slide that fails goes back to the model with the reason:

> a text box on your slide is too small for its text (it needs 1.42x the height): make the box
> taller, the text shorter, or the font smaller

If every attempt fails, the deck is delivered without that slide and the result says so. The
previous version is still in the bucket, so nothing has been lost.

## What it looks like with a real model

From `tests/validate_real_model.py --code` against `gpt-6.1-sol`:

```
write_slide_code (the escape hatch)
  2212 characters; A spacious title sits above three aligned columns, each with a coloured
  rule, bold heading, and supporting text.
  passed the static gate
  ran in 0.56s, slides 9 -> 10
  resealed: 51 parts byte-identical
  the slide it drew passes the overflow and overlap checks
```

## Widening it

The facility is scoped to one contract - `build(prs, ctx)` - rather than "run arbitrary Python".
That is not a limitation of the sandbox, which runs whatever argv it is given; it is a choice
about what the *graph* will ask for. Widening it means adding a harness with its own contract
and its own checks on the result, and the thing to keep is the shape: a narrow contract, a
static gate that produces feedback, a container that is the real boundary, and an independent
check on the output. Code that runs and produces something nobody verified is the failure mode
to avoid.
