# 8. Status, measurements, and what is still missing

Honest, in the style of the handoff document this was built from.

## What was built

| Area | State | Notes |
|---|---|---|
| Single-process engine (probe, compile, apply, byte-preserving save, checks) | **Works** | No subprocess. 84 unit tests pass with nothing running |
| Kit building in-process (draft → label → accept → measure capacity) | **Works** | 9/9 patterns accepted on the bundled template |
| LangGraph `StateGraph`, 17 nodes, named decisions, trace | **Works** | `make graph` prints it from the code |
| Postgres checkpointer, conversations across processes | **Works** | Tested across separate graph instances and pools |
| Object storage, versions, undo, presigned links | **Works** | SeaweedFS locally; plain boto3, so any S3 |
| **Router** (rules first, model when unsure, ask when neither) | **Works** | The gap the handoff listed first |
| Create route (two stages, retries, repair) | **Works** | Real model: 1.00 attempts per slide |
| Translate route (tagged runs, shorten then font fallback) | **Works** | |
| **General edit route** (set_text, delete, relayout_cards, insert_slide) | **Works** | The gap the handoff listed second. `insert_slide` now checks and repairs the slide it adds |
| **Vision review** of every rendered slide, feeding the repair loop | **Works** | Off by default; 0 false alarms on a clean deck, found both real defects cold |
| **Layout variety** across a deck | **Works** | Menus narrowed before stage 2, repetition caught after |
| **Layout change** (`change_layout`) | **Works** | Same words, a different layout from the deck's own; not for code-drawn slides |
| **Visual changes** (`set_style`, `set_theme_colour`) | **Works** | Not in the engine's op language; a deterministic pass of its own, byte-preserving |
| **Sandboxed code escape hatch**, resealed and checked | **Works** | Byte-identity of untouched slides asserted |
| **No-fit fallback** (relaxed options before generated code) | **Works** | Partial - see below |
| **Kit cache** by template content hash | **Works** | |
| **Real-model validation** of every call | **Works** | 6/6 against `gpt-6.1-sol`, vision included |
| Outline approval pause | **Built, off by default** | |
| Restyle | **Declined, with a reason** | Recognised and refused rather than faked |
| Picture slots, icons, diagrams | **Missing** | Unchanged from the engine |
| Facts enforcement (numbers checked against cited facts) | **Missing** | Citation markers are now stripped from visible text, which is not the same thing |
| Structured output (per-slide JSON schema) | **Missing** | JSON mode plus the compiler's retry loop |

## Measured

**Tests.** 107 unit (no containers, no Postgres, no model), 18 integration (Docker, Postgres,
S3), and the engine's own 28 still pass unmodified. `docs/09-testing.md` is honest about where
the coverage is thin and about four things that were wrong until they were actually run.

**The sandbox**, from inside itself:

```
network: blocked (OSError)   dns: blocked (gaierror)   rootfs: read-only (OSError)
write_work: yes   uid: <the host user>   soffice: /usr/bin/soffice   pdftoppm: /usr/bin/pdftoppm
```

**Byte preservation through the escape hatch**: model-written code added slide 8 to a real
7-slide deck; all 7 original slides byte-identical, 3 container parts changed, 2 added.

**Rendering**: 1.5 s for a 2-slide render, container start included.

**Real model** (`gpt-6.1-sol`), `tests/validate_real_model.py --code --vision`:

```
ok  route                                17.5s     5/5 requests routed as expected
ok  outline (stage 1)                    12.6s     6 slides, every cited fact real
ok  render_slide (stage 2)               20.9s     6/6 placed, 1.00 attempts per slide
ok  plan_edit                            10.9s     including "turn these 8 steps into 6"
                                                   -> relayout_cards -> 41 ops
ok  write_slide_code (the escape hatch)  14.7s     passed the gate, ran, resealed, checked
ok  label_slide (vision)                 17.2s     4 slides labelled, every role and kind valid
```

The handoff's acceptance bar for stage 2 was "under one retry per slide on average". It is
1.00 attempts - no retries at all - on the bundled template.

**A real deck**, built with that model through the full stack: 5 slides, 0 warnings, 0 leaked
template content, a real chart drawn from the dataset, previews rendered.

## Two defects found by looking at rendered slides

Both invisible to the checks, because neither is a fitting problem. The engine's working notes
say to look at the PNGs before every commit; this is why.

**1. Orphaned decoration.** The chart pattern's side text sits on a tinted panel. When a plan
fills the chart but no side text, the compiler correctly deletes the empty text boxes - but the
panel holds no text, so the leak guard keeps it, and the slide has an empty grey box where the
commentary would have been.

Fixed in `engine/tidy.py` with a conservative geometric rule: a shape with no text, not kept by
the kit, is dropped only when it encloses at least one of the pattern's field boxes and *every*
field box it encloses was left empty. Global decoration - an accent bar, a hairline above the
footer - encloses no field box and is never touched. There is a test asserting that thin
decoration survives.

**2. Citation markers on the slide.** Asked to use only the content pack and cite it, the model
wrote the citation into the visible words: *"The bot answered 38% of incoming tickets. [F2]"*.
The fact id belongs in the plan's `fact` field, where it can be checked; on a slide it is
embarrassing. Stripped deterministically in `engine/tidy.py`, bracketed forms only, with the
`fact` and `sources` fields left intact.

## What the engine itself got wrong, or does not do

Reported back rather than worked around silently.

**The handoff's §7 is wrong about `expect`.** It documents `"expect": {"sha": "..."}`. The
engine's `deckops/ops.py` requires `text_sha`, and the executor checks that. An op written from
the document is rejected by pydantic. (It also writes the table key as `{"id": 8}` where
`validate_kit` and `kit_check` use `{"shape": 8}`.)

**A slide with no items fits nothing.** `options_for` requires a pattern's item count to be
within `[min, max]`, and `compile_from_kit` enforces it. In the automatically built kit for the
bundled template, every pattern either needs items (`cover` 2-4, `stats` 2, `steps` 1,
`columns` 2, `panels` 1) or is position-locked (`closing` is last-slide only) or is a table or
chart layout. So **a cover slide with a title, a subtitle and no bullets fits nothing at all**
and goes straight to the escape hatch. This is the handoff's gap 6 in practice, and it is not
an exotic case.

`engine/fallback.py` handles it in this layer by relaxing in named steps, but the proper fix
belongs in the engine: let a pattern with items accept zero of them by deleting the item
shapes, the way an unfilled field is already deleted.

**`add_slide_from_pattern` cannot be positioned.** `AddSlide` takes `after`; the pattern version
does not, so a copied slide always lands at the end. Inserting a slide therefore needs a second
deterministic pass (`deck.move_slide`, a `p:sldIdLst` reorder). An `after:` field on
`AddSlideFromPattern` would remove the need.

**`execute()` and `save_preserving()` report `parts.preserved` differently** - a count from one,
a list of names from the other. `engine/deck.preserved_count()` normalises it.

**A kit written by the CLI carries a `_sandbox` key** from the sandbox report. Harmless, and it
disappears here because there is no sandbox report.

## Found by running the paths that had never run

**`insert_slide` produced a broken slide the first time it executed** - headings wrapped over
five lines inside 30-point circles. Three fixes: `light_kit` now always runs the engine's
`role_problems`; a new `badge_problems` drops a pattern whose word-carrying field is pointed at
a badge rather than a line of text; and the edit route gained the repair loop it was missing, so
a slide that does not fit is retried and then abandoned rather than delivered broken.

**The offline demo's translate applied zero operations** and still looked green, because the
stub planner is a phrasebook, not a translator. Verified against a real model instead: 6
operations, real French, 54 parts byte-identical.

## The limit that "add a title slide" ran into

Insert and relayout use **the deck as its own template** - they have to, because
`add_slide_from_pattern` copies a slide *within one file*, so the template's example slides are
not there to copy. The consequence is sharp and worth stating: **you can only move a slide to a
layout the deck already contains.**

This deck's cover was built with the `panels` layout, so the deck holds no cover pattern at
all. Asked for a title slide, the honest answers available were "slide 1 already has that
heading" (which is what it now says) and a move to `steps`, which put a numbered badge on the
title slide - worse than leaving it alone.

**This is now fixed** by rebuilding from the plan. `change_layout` re-plans the slide onto one
of the *template's* layouts and builds the deck again, so the full kit is reachable; slides
drawn by generated code are replayed from the code and context stored when they were first
drawn. Measured on a deck whose own layouts were only `cover`, `steps` and `panels`: slide 5
moved to a layout the deck did not contain, the generated slide came back at position 4
unchanged, and the deck passed every check with no warnings.

It is guarded rather than unconditional. A rebuild is only safe while the plan still describes
the deck, so every node that edits bytes without updating the plan records what it did in
`plan_divergence` - text edits, colour changes, locally inserted slides, a translation - and a
rebuild that would throw those away is refused, naming them. When it is refused, the deck's own
layouts are used instead, exactly as before.

The alternative - copying the template's example slide into the deck, a cross-file slide copy -
is still not built, and would be the way to do this without a rebuild.

## What a real commercial template did

The first one tried - a bought "Challenges and Solutions" deck, 24 MB, 20 slides, 52-131 shapes
a slide - produced **zero usable patterns**. The ingestion is automatic and said so clearly;
what it found was three independent blockers, in this order:

1. **Duplicate shape ids on 15 of 20 slides.** Refused, correctly: the engine addresses
   `slide:<id>/shape:<id>` and two shapes with one id cannot be told apart. But every duplicate
   was *inside an embedded think-cell object*, and among the top-level shapes - the only ones
   the engine touches - there were none. `engine/normalise.py` renumbers those nested ids and
   leaves every top-level id alone; verified on this template with all 18 OLE objects and all
   27 embedded parts byte-identical, and all text and geometry unchanged.
2. **Grouped shapes on 19 of 20 slides.** **Now fixed** - see below. A shape
   inside a group reports its position in the group's own coordinate space, so the geometry the
   drafting rules reason about is meaningless and they skip the slide. Flattening the groups was
   tried and rejected - see the note in `engine/normalise.py` for why, and for where the fix
   belongs (`probe`, emitting slide-space geometry for nested shapes, which is a small change
   to the engine and leaves the template untouched).
3. **An embedded object on the one slide that survived.** The executor will not clone a slide
   holding a chart, SmartArt or an OLE object, so even the cover was dropped by the acceptance
   test.

Two things were fixed by trying: the kit build used to **crash** with an unhandled `Refused`
when a pattern could not be built, instead of dropping that pattern with a reason - the whole
point of an acceptance test - and the duplicate-id normalisation above.

## Group support, and what it unlocked

Group support is in, as `patches/0001-group-support.patch` - the first and so far only change
to the engine, because it is the one thing composition could not reach: the rules deciding
whether a slide can become a layout live inside the engine and read the file themselves.

On that same commercial template, with nothing else changed:

| | before | after |
|---|---|---|
| candidate slides drafted | 1 | 17 |
| distinct slide kinds | 1 | 6 |
| **patterns accepted** | **0** | **6** - cover, cards, columns, panels, stats, text |
| decoration kept | - | logo, cover photograph, icons, tick marks |
| decks buildable | none | yes, 0 findings, 0 leaked content |

Three slides are still skipped, each holding a real chart, which remains out of scope.

Getting there took four things, and each was a separate wrong assumption:

* slide-space geometry for nested shapes, so the pattern rules compare like with like;
* writing a position *back* into a nested shape's own space, and rewriting a shape's geometry
  when it is duplicated out of a group onto the slide;
* an embedded object no longer blocking a slide from being copied - commercial decks park
  add-in data (think-cell) on every slide, and treating that like a chart refused whole
  templates. Charts and SmartArt still block copying, and the object is dropped from the copy;
* a picture the pattern itself uses is no longer counted as a leak, because with groups usable
  an item is routinely "icon + heading + body" and the icon is meant to be copied per item.

## Still missing, in the order worth doing

1. **Group support in `probe`.** Emit slide-space geometry for shapes nested in groups;
   `_shape_entries` already recurses into them and gives each an addressable ref, so only the
   coordinates are wrong. Then the drafting rules can stop skipping those slides.
   *Done:* a commercial template with grouped icon-and-text cards yields usable patterns.
2. **Facts enforcement.** The content pack is given and cited, but no number on a slide is
   checked against the fact it cites. Stripping citation markers improved the look, not the
   truth. *Done* means an invented number is rejected and the slide re-rendered.
2. **Structured output per slide.** Build a JSON schema from the options (pattern enum, allowed
   fields per pattern) and use strict schema where the endpoint supports it. Today a wrong
   field name costs a retry - cheap, but avoidable.
3. **The no-fit fix in the engine**, as above, so generated code is reserved for genuinely
   novel slides rather than ordinary title slides.
4. **Fan-out for stage 2 as `Send` nodes.** Today `render_slides` parallelises internally with
   four threads. Per-slide LangGraph nodes would give per-slide durability and streaming
   progress, at the cost of bigger checkpoints. Worth it when decks get long.
5. **Restyle.** Probe the old deck, outline from its content, create in the new kit. All the
   pieces exist; it is a route, not a feature.
6. **Picture slots.** A box marked "image" in a template becomes a slot; the agent supplies an
   image and the aspect ratio is checked.
7. **Fewer items than the example** still stretch to fill the area and can look sparse (the
   engine's gap 10).
8. **Checkpoint and artefact pruning.** Both accumulate for ever today.
9. **A recorded-response planner** so translate and edit have end-to-end tests that fail on a
   regression. Today they are verified by hand against a real model, which no test enforces.
10. **Apply `relayout_cards` to a deck and look at it.** It compiles to 41 operations with a
    real model; nobody has run those operations and inspected the result.
