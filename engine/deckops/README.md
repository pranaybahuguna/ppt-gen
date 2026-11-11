# deckops: LLM-driven PowerPoint generation and editing (POC)

The model never writes a .pptx. It writes a **plan** (what the deck should say). A pure
**compiler** turns the plan into typed **operations**, and a sandboxed **executor** applies them
to the deck. Untouched slides come out byte-identical, every change is an auditable op, and
the model never has to produce coordinates or XML.

## How to run

Needs Python 3.11+, LibreOffice (renders slide previews), poppler (PDF to PNG) and the
Carlito/Liberation fonts (same letter widths as Calibri/Arial, used to predict text overflow).

**macOS** (Homebrew):

```
brew install python@3.12 poppler
brew install --cask libreoffice font-carlito font-liberation
open -a LibreOffice          # once, so macOS lets it run; then quit it

git clone https://github.com/pranaybahuguna/deckops && cd deckops
python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

**Linux** (or WSL2 on Windows):

```
sudo apt-get install -y libreoffice-impress poppler-utils util-linux fonts-crosextra-carlito fonts-liberation
git clone https://github.com/pranaybahuguna/deckops && cd deckops
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

Then, on either:

```
python3 -m pytest -q tests        # 26 tests, ~3 s
python3 run_demo.py               # full pipeline offline; outputs in out/
```

The demo's first line must say `network inside sandbox: blocked`. If it says `REACHABLE`, stop:
the sandbox isn't isolating on this machine.

**The sandbox differs by system.** Linux uses `unshare` (no network at all) and `prlimit`
(memory, CPU, file-size limits). macOS uses the built-in `sandbox-exec` with a profile that denies
all IP traffic, plus CPU, file-size and open-file limits; macOS cannot cap memory this way.
Apple marks `sandbox-exec` as deprecated, though it still ships. On both, the process can still
*read* the host's files. Fine for trying it out; production uses a microVM (see below).

On your own template:

```
# 1. once per template: build and test a style kit (no person involved)
python3 deckops_cli.py kit my_template.pptx my_kit.json

# 2. new deck in that look (a model writes the plan from the kit and your content pack)
python3 deckops_cli.py create my_kit.json content.json "brief for the deck" new.pptx --llm llm.json

# 3. translate slides 1, 2 and 5
python3 deckops_cli.py translate deck.pptx fr 1,2,5 deck_fr.pptx --llm llm.json
```

`content.json` holds the facts and datasets the deck may use (see `CONTENT` in
`examples/create_from_style_kit.py`). Without `--llm`, `create` needs `--plan plan.json` and
`translate` uses an offline stub that only knows the demo's strings. Every run keeps its plans, ops,
reports and slide previews in `runs/<id>/`.

## Checking it against your model

```
python3 tools/llm_check.py my_template.pptx llm.json            # uses samples/content_example.json
```
Builds the kit (with vision labels) and a two-stage deck, prints a summary (vision changes and vetoes,
retries, repairs, findings) and writes `llm_check_results.zip` with the reports, previews and deck.
`llm.json` is never put in the zip. The HTTP client is tested against a local fake server
(`tests/fake_llm.py`), not a real model.

## Commands (reference)

```
python3 run_demo.py                          # end-to-end run with an offline stub planner (~6 s)
python3 run_demo.py --llm-config llm.json    # use a real OpenAI-compatible model
python3 -m pytest -q tests                   # pure-function tests: no model, no sandbox
```

`llm.json` (git-ignored): `{"base_url": "https://…", "model": "…", "api_key": "…"}`.
No environment variables are read anywhere.

**Requirements:** macOS or Linux, Python 3.11+, `python-pptx`, `lxml`, `pydantic`, `Pillow`, `pytest`,
LibreOffice (`soffice`), `pdftoppm` (poppler), and for the sandbox `unshare` + `prlimit` (Linux)
or the built-in `sandbox-exec` (macOS).

## What the demo proves

| Check | Result |
|---|---|
| Network reachable inside sandbox | blocked |
| Translate slides 2–5 to French (mixed-format runs, line break, table, group shape, notes) | valid deck |
| Untouched slides, master, images | byte-identical to input |
| Translation overflows a text box | detected; repaired by model shortening, else font-size fallback |
| Op whose target changed since planning (stale fingerprint) | whole batch refused, nothing written |
| Zip bomb (600 MB unpacked, 600 KB on disk) | rejected before any parser runs |
| Create a 4-slide deck from a template by role (title/body), no coordinates | valid deck |
| Edit: 8 process cards → 6 cards in a 3×2 grid, longer text | only that slide's XML changes |
| Create a 9-slide deck in an example "house style" deck's look, incl. stacked column, pie and line charts from data | valid deck, real editable charts, no example text left, file properties reset |

The model step is a deterministic stub unless `--llm-config` is given.

## Architecture

```
            request + deck                        plan JSON
  User ───────────────────▶ CONTROL PLANE ◀──────────────────▶ LLM
                            orchestrator / planner / compiler
                            (never opens a .pptx; JSON only)
                                    │  sandbox.run(cmd, file names)  ▲ JSON report
                                    ▼                                │
                            DATA PLANE (sandbox: no network, rlimits)
                            dataplane_main → zipguard → probe / executor / validate(+LibreOffice)
                                    │
                            WORKSPACE (local folder per run; S3 prefix in production)
                            source.pptx  index.json  plan.json  ops.json  out.pptx  reports  previews
```

Pipeline: **intake → probe → plan → compile → execute → validate → repair (≤2) → deliver**.
The model appears only in *plan* and *repair*. Compile is a pure function, and execute always replays from
the original source.

## Code map

| Module | Runs in | Responsibility |
|---|---|---|
| `deckops/refs.py` | both | NodeRef addressing (`slide:256/shape:4/para:0/run:1`), fingerprints, tagged-run text |
| `deckops/probe.py` | sandbox | .pptx → DeckIndex JSON (slides, shapes, text, tables, notes, layouts, opaque objects, external links) |
| `deckops/ops.py` | both | The op language: pydantic models for each operation |
| `deckops/compiler.py` | control plane | Plan → ops, pure: `compile_translate`, `compile_create`, `compile_card_grid` |
| `deckops/executor.py` | sandbox | Bind all targets + check fingerprints, apply in phases, save keeping original bytes |
| `deckops/validate.py` | sandbox | Structure checks, font-metric overflow prediction, LibreOffice previews |
| `deckops/zipguard.py` | sandbox | Archive safety checks before parsing |
| `dataplane_main.py` | sandbox | The only entry point that reads deck bytes |
| `controlplane/sandbox.py` | control plane | Launches the data plane isolated. **Swap point for a microVM runtime** |
| `controlplane/planner.py` | control plane | Model step: offline stub + OpenAI-compatible client |
| `controlplane/orchestrator.py` | control plane | `translate()`, `create()`, `edit()` pipelines |
| `deckops/patterns.py` | control plane | Style kits: compile "copy example slide + fill" plans; grid re-flow for repeated items |
| `examples/edit_card_grid.py` | — | Edit-mode example (8 cards → 6) |
| `examples/create_from_style_kit.py` | — | Create a new deck in an example house style |
| `samples/make_style_template.py` | — | Builds an example house-style deck + its style kit |
| `tools/inspect_deck.py` | local | Lists every shape id on every slide: what you need to write a style kit |

### Op language (current)

`set_paragraph_text` · `set_run_text` · `set_font_size` · `set_geometry` · `delete_shape` · `add_slide` · `delete_slide`
· `add_slide_from_pattern` (with steps `duplicate_shape`, `delete_shape`, `set_geometry`, `set_shape_text`, `set_table`, `set_chart`) · `set_doc_properties`

Adding a capability = an op model in `ops.py` + bind/apply in `executor.py` + a compile function.

## Creating decks in your house style

### Two kinds of template

**Placeholder templates.** The template's *layouts* have empty, named slots ("title", "body").
A new slide picks a layout and fills its slots, and the look comes from the layout. `create()` uses this,
matching each planned slide to a layout by the slots it needs.

**Example-slide templates.** Many real corporate decks have *no* slots. Every title and box is a
hand-placed shape with fixed colours, all on one blank layout (common with decks made by code, or
copied around for years). There the design lives in the **example slides**. So deckops copies a good
example slide and swaps its content. `create_from_kit()` does this.

### Style kit

A small JSON file written once per template. It says which example slide to use for what, and
which shape on it holds which piece of content. Example (abridged):

```json
{
  "name": "Example house style",
  "template": "example_style.pptx",
  "page_format": "{n} / {total}",
  "patterns": {
    "steps": {
      "slide": 3,
      "use_for": "a numbered process of 3-8 steps",
      "fields": {"title": 3, "subtitle": 4, "footer": 6, "page": 7},
      "items": {"groups": [[8, 9, 10, 11], [12, 13, 14, 15]],
                "fields": {"number": 1, "heading": 2, "body": 3},
                "min": 3, "max": 8, "max_columns": 4}
    }
  }
}
```

- `fields`: content name → shape id. `page` and `footer` are filled automatically.
- `items`: a repeated block (cards, columns, stat tiles, panels). `groups` lists the shape ids of each
  example item; `fields` maps content names to a position inside a group.
- `table`: a table shape plus a row limit.

### What happens when a deck is created

1. **Probe** the example deck in the sandbox: every slide, shape id, position and text.
2. **Plan, in two stages** (`controlplane/slide_flow.py`):
   - **Stage 1, content (one model call).** From the content pack, the model writes what each slide
     says: purpose, title, points, figures and the facts they come from. No layouts, no field names.
   - **Stage 2, layout (one call per slide, in parallel).** Code works out which layouts can hold the
     slide (item counts measured per template; table and chart slides only get table and chart
     layouts; cover only first, closing only last). The model gets the slide's content plus only those
     layouts, picks one and fills its fields. A slide with more points than any layout holds is split
     into "(1/2)", "(2/2)".
   - **Errors go back to the model, per slide.** Each rendered slide is compiled on its own; an error
     ("needs 1-8 items, got 10") is sent back with the previous answer, up to 2 retries. After the
     build, text that doesn't fit goes back the same way ("rewrite it in at most 48 characters"), and
     only that slide is redone.
   - Without a model: `--outline outline.json` supplies stage 1 and a rule-based renderer does stage 2.
     `--single-call` keeps the old one-call planning.
3. **Compile** (pure code). For each planned slide:
   - copy the example slide;
   - if there are more items than the example has, duplicate an item's shapes; if fewer, delete the extras;
   - re-flow the items in a grid inside the area the example's items used. Small shapes such as circles
     keep their size, and boxes that reach the item's edge stretch;
   - write the text: each new paragraph copies the formatting of the example paragraph, so bullets, font
     and colour survive;
   - **delete any example text shape the plan didn't fill**, so example content can't leak.

   Then delete all original example slides and reset the file properties (title, author).
4. **Execute** in the sandbox, **validate** (overflow compared to the example slide's own baseline), and
   **scan** the result for any remaining example text.

### Charts: the model chooses, the data decides

The deck may only use data from the **content pack**, which the agent layer gathers before planning:

```json
{"datasets": {"requests_by_team": {"source": "usage DB",
   "columns": ["Team", "Requests (k)"],
   "rows": [["Markets", 410], ["Banking", 260], ["Securities", 180]]}}}
```

A chart slot on an example slide is just a rectangle marking the chart area. The model's plan says
*which* data and *what kind* of chart, never the numbers:

```json
"charts": {"main": {"type": "bar", "dataset": "requests_by_team",
                    "category": "Team", "series": ["Requests (k)"], "number_format": "#,##0"}}
```

The compiler copies the numbers from the dataset and checks them: the dataset and columns must
exist, values must be numbers, a pie/doughnut takes exactly one series, no negatives, at most 8 slices.
The executor replaces the rectangle with a **real, editable PowerPoint chart** (data in an embedded
workbook) coloured from the kit's `chart_style.palette`. Types: bar, column, stacked_column, line,
pie, doughnut.

Tables work the same way (header + rows, styled like the example table). Repeated cards and tiles
re-flow as a grid.

### Your own colours

`samples/make_style_template.py OUT_DIR brand.json` builds the example template in your colours and font
(see the docstring for the keys). That's useful to try things out. For production, start from your real
template and write a kit for it.

### Getting a kit for your own template (automatic, no person involved)

```python
from controlplane import orchestrator
from controlplane.planner import StubPlanner            # or OpenAICompatPlanner.from_file("llm.json")
run, kit, report = orchestrator.prepare_template("runs", "my_template.pptx", StubPlanner())
```

`prepare_template` runs once per template:

1. **Draft (sandbox, rules only)**, one candidate per usable slide. `deckops/autokit.py` reads every slide: title/subtitle/footer/page
   by position and size, repeated items (the same box shape repeated 2-12 times, with the same
   neighbours at the same offsets), chart areas (big empty boxes), tables. It names fields by what
   the box can hold (a box with room for under 1.8 lines is never "body"), and takes the chart palette
   from the slides' fills and the theme. Pictures that appear on at least half the slides (logos) are
   kept; every other picture is content and is removed when the slide is reused.
   A headline value must be a short figure with a digit ("42%", "3.2x"); slides left with only a
   title and footer to fill (the rest is a picture) are not offered.
2. **Check the ids** (`validate_kit`): every id exists, item groups line up.
3. **Vision labels, optional (control plane).** The sandbox renders each candidate slide twice:
   plain, and with every fillable box outlined and numbered. The control plane (never the sandbox)
   shows the numbered picture plus the rules' guesses to a vision model, which says what each box is
   for, what kind of slide it is, and whether it's worth offering. `controlplane/kit_merge.py`
   marries the two: **structure from the rules** (which boxes, which repeat), **meaning from vision**
   (names, kind, description), and **physical vetoes** on vision (a one-line box can't be "body"; a
   number must count 1, 2, 3; a value needs a digit; long text can't be kept as decoration). Every
   decision is saved in `label_decisions.json`: agree, vision, or vetoed and why. Then the first
   slide of each kind is kept.
4. **Acceptance test** (`controlplane/kit_check.py`): fills every pattern with dummy text at the
   example's length (shortest and fullest variants), builds one test deck, validates it (overflow,
   overlap, leaks), and drops any pattern that fails. With a vision model, it also compares each test
   slide with its example.

Each run saves the model's raw answers as `vision_labels.json`; `--labels vision_labels.json`
replays them instead of calling a model (handy for tests or
to re-run without cost).

Output: `kit.json` (passing patterns only) and `kit_report.json` (why each pattern passed or failed).
`tools/compare_kits.py template.pptx reference_kit.json` compares an automatic kit with a hand-written
one; on the bundled example template it matches exactly. `samples/make_style_template.py` builds that
example (deck + kit). Keep a real company template and its kit **outside** this repository.

The rules were tuned on two templates. A template with unusual layouts may get fewer patterns
(failing patterns are dropped, not guessed); the report says why.

Limits today: example slides that already hold charts, SmartArt or embedded objects can't be used as
patterns (use a plain rectangle as the chart area instead).
Tables must keep the example's column count. Overlap between text shapes is detected; overlap with
pictures or decoration is not.

## Design decisions found by building it

1. **Byte preservation needs help.** python-pptx re-serializes every part it parses. `save_preserving`
   compares parts by meaning (canonical XML; relationships with resolved targets; effective content
   types) and writes the original bytes for anything unchanged.
2. **Translate by paragraph, not by run.** Word order changes across languages. Paragraphs go to the
   model as tagged runs (`<r0>…</r0><r1>…</r1>`), and runs may be reordered while each keeps its formatting.
3. **Bind, then apply.** All targets are resolved against the source before any mutation, so ops in one
   batch can't shift each other's positional indices.
4. **Validate differentially.** Overflow prediction is approximate, and real decks have pre-existing problems.
   Only issues the edit made worse are reported.
5. **Autofit differs by renderer.** PowerPoint shrinks text on edit; LibreOffice doesn't. Findings carry
   `masked_by_autofit`.
6. **The model never emits geometry.** Layout (e.g. re-flowing cards into a grid) is computed by the compiler
   from the slide's own margins and gutters.

## Sandbox: POC vs production

On macOS the same `sandbox.run()` uses `sandbox-exec` instead (see How to run).
The POC isolates the data plane on Linux with `unshare -rn` (user + network namespace: no network) and
`prlimit` (memory/CPU/file-size caps), a scrubbed environment and a timeout. It does **not** isolate the
filesystem or the kernel. For production, replace `controlplane/sandbox.py:run()` with a microVM runtime
(e.g. Kata Containers / Firecracker). Replace the local workspace with object storage that the VM downloads
from and uploads to. The command-line + JSON contract stays the same.

## Orchestration

The orchestrator is plain Python functions on purpose. The pipeline is a fixed chain with one bounded
loop and the model at two points. In production, wrap the stages as nodes of whatever workflow
framework the host application already uses (e.g. pydantic-graph or LangGraph), with persisted state so a
run survives a restarted worker. LangChain isn't needed: the model calls are structured-JSON requests
validated by pydantic.
