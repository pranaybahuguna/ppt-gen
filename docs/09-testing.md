# 9. How this was tested, and where the coverage is thin

Written because "it works" is not a claim anyone should accept, including me. Several things in
this project were wrong until something was *run*, and the sections below say which.

## The four layers

| Layer | Command | Needs | Count |
|---|---|---|---|
| Unit | `make test` | nothing running | 244 |
| Integration | `make test-all` | Docker, Postgres, S3 | 19 |
| The engine's own | `make test-engine` | nothing | 28, with this project's patch applied |
| Offline conversation | `make demo` | Postgres, S3 | every route, no model |
| Real model | `tests/validate_real_model.py` | `llm.json` | every model call |

Unit tests pass with **nothing running** - no containers, no Postgres, no object storage, no
model. That is only possible because the single-process design leaves almost everything pure;
it is a consequence of the architecture, not an achievement of the tests.

## What is covered end to end, and by what

| Path | Offline (stub) | Real model | Looked at by eye |
|---|---|---|---|
| create → kit → outline → render → assemble → deliver | yes | yes, a 5-slide deck | yes |
| repair loop (character budget) | yes | not needed - 0 retries | – |
| model-written code: code → container → reseal → check | yes | yes | yes |
| gather: context → facts, quotes checked against the source | yes | yes, a README into 33-40 facts, 0-1 dropped | – |
| layout budget → slides reworked in place by code | yes, in the sandbox | yes, 6 of 12 slides on the README deck | yes - the frame version was broken; see 08-status |
| clean-up: tidy, purge, orphans, everything; tests clean up after themselves | yes | – | – |
| translate: tagged runs → ops → apply | **see below** | yes, 6 ops of real French | – |
| edit: `set_text` | yes | yes | – |
| edit: `relayout_cards` | compile only | yes, 41 ops | not yet applied to a deck |
| edit: `insert_slide` | no | yes | yes - and it was broken |
| edit: `set_style` / `set_theme_colour` | yes | yes | yes |
| vision review → findings → repair | wiring only | yes, on good and bad decks | yes |
| router, ask/answer, decline, undo | yes | yes | – |
| checkpoint across processes | yes | – | – |

## Four things that were wrong until they were run

**The offline demo's translate applied zero operations.** The stub planner has a small French
phrasebook, not a translator, so none of the deck's text matched and `translate_apply` ran
without changing anything. The route looked green and proved almost nothing. Running it against
a real model gave 6 operations, real French (*"Q3 2026 Business Review"* → *"Revue d'activité du
T3 2026"*) and 54 parts byte-identical. The demo now says out loud that the stub is a phrasebook,
so a reader is not misled by a clean run.

**`insert_slide` had never executed at all.** It was written, documented and unexercised. The
first real run produced a genuinely broken slide: headings wrapped over five lines inside
30-point circles, overlapping their neighbours. Two defects behind it:

* `light_kit` skipped `role_problems`, the engine's check that a prose field sits in a box
  holding at least two lines. That check is *pure and free*; skipping it was wrong, and it is
  now always run.
* It still was not enough. The drafting rules had mapped the item's `heading` onto the
  **number circle** (50×50pt, aspect 1.00) and the real heading box onto `detail_1`.
  `role_problems` cannot see that, because `heading` is not a long-prose role and two short
  lines genuinely do fit in a circle. So `badge_problems` was added: a field whose role carries
  words must sit in a box meaningfully wider than tall, or tall enough to be a paragraph -
  `value`, `number` and `page` exempted, since a figure in a square tile is right. It drops the
  bad pattern on the sample deck and leaves all nine patterns of the properly built kit clean.
* The edit route had **no repair loop**, so a slide that failed the checks was delivered with a
  warning nobody could act on. It now retries with the same character-budget feedback the create
  route uses, and if the slide still does not fit, the insert is abandoned and the deck is left
  as it was - better than handing over something broken.

**Two defects were visible only in the rendered PNGs.** An empty grey panel surviving on chart
slides, and `[F2]` citation markers in visible slide text. Neither is a fitting problem, so no
checker would ever have reported them. `docs/08-status.md` has the diagnosis of both.

**The in-memory checkpointer was not a checkpointer.** It built a fresh saver per call, so the
second turn of a conversation found no thread. A graph test caught it.

## The eyeball step is now automated

Every defect in the section above was found by looking at a rendered slide. That step is now a
graph node (`look`), and it was calibrated before being trusted:

* **on the two known-bad slides**, asked cold, it named both defects exactly - the empty panel
  and the headings crushed into circles;
* **on a four-slide deck that passes every geometric check**, 0 to fix and 0 noted - no false
  alarms, which is the result that decides whether it is safe to act on;
* **at 60, 110 and 160 DPI** it reported the same defect, so the previews the build already
  renders are good enough and no second render is needed;
* **on the template's own example slides** it reports the placeholder text. That looked like a
  false positive until you remember those slides *are* placeholders - it is a second, free
  check on the engine's leak guard.

What is tested deterministically (`tests/unit/test_look.py`) is the wiring, not the judgement:
high severity becomes a finding and routes to repair, low severity only notes, a reviewer that
throws cannot invent problems or block delivery.

## What that says about the tests

The unit tests are good at compilers, routing, refusals and fingerprints - things with a right
answer. They were useless against all four problems above, because each needed either a real
model, real bytes, or a pair of eyes. The order that actually works:

1. unit tests for anything with a right answer;
2. run the path end to end, with a real model, at least once;
3. **look at the rendered slides** (`make demo`, then fetch the previews).

Step 3 is the engine author's own rule, in their §15. It is right.

## Thin spots, named

* **`relayout_cards` has never been applied to a deck.** It compiles - the real model turned
  "turn these 8 steps into 6" into 41 operations - but nothing has run those operations and
  looked at the result. The highest-value next test.
* **Translate has no automated end-to-end assertion.** It was verified by hand against a real
  model; there is no test that fails if it regresses, because the stub cannot translate. A
  recorded-response planner, like the engine's `RecordedVision`, would fix this.
* **`approve_outline`** is implemented and never tested; no test turns it on.
* **One template.** Every kit result comes from the engine's bundled example template. The
  engine's author tuned the drafting rules on three real templates; this project has seen one.
  Expect surprises on a real corporate deck - and expect them in kit building, which is where
  template variety bites.
* **`custom_code` failure paths**: the success path is tested end to end, and the guard and
  container limits are tested, but "the model wrote code that passed the gate, ran, and
  produced a bad slide three times running" has not been exercised.
* **Concurrency.** Two turns for the same `deck_id` at once is untested. LangGraph's
  checkpointer has opinions about this; this project has not formed one.

## Running the real-model validation

```bash
.venv/bin/python tests/validate_real_model.py           # route, outline, render, edit
.venv/bin/python tests/validate_real_model.py --code    # + the escape hatch
.venv/bin/python tests/validate_real_model.py --vision  # + kit labelling from pictures
```

It makes one call of each kind, checks the answer is the shape the code downstream expects, and
prints what came back, so a prompt that drifts shows up as a named failure rather than as a
strange deck three steps later. Last run, against `gpt-6.1-sol`:

```
ok  route                                17.5s   5/5 as expected
ok  outline (stage 1)                    12.6s   6 slides, every cited fact real
ok  render_slide (stage 2)               20.9s   6/6 placed, 1.00 attempts per slide
ok  plan_edit                            10.9s   including relayout_cards -> 41 ops
ok  write_slide_code (the escape hatch)  14.7s   gate, ran, resealed, checked
ok  label_slide (vision)                 17.2s   4 slides, every role and kind valid
```
