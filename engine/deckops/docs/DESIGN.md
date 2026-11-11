# deckops: design and step-by-step walkthrough

How an AI assistant creates, edits and translates PowerPoint decks safely.
This document follows **one real slide** through every step and shows exactly what each step
receives, what it does, what it writes, and how the next step uses it. All excerpts come from an
actual run (the "chart" slide of an 8-slide deck).

---

## 1. The idea in three sentences

1. The **model only fills in a plan**: which kind of slide, and what words go where. It never writes
   PowerPoint XML, positions, colours or chart numbers.
2. **Ordinary code** turns that plan into exact instructions and carries them out by copying example
   slides from a template and filling them in. Same input, same output, every time.
3. Every step that opens a PowerPoint file runs in a **sandbox** (no network, limited memory and
   time), and every result is **checked** before anyone sees it.

---

## 2. The big picture

```
 ┌────────────────────────── LAYER 1: AGENT (the chat assistant) ───────────────────────────┐
 │  user request + attachments  →  tools (files, MCP, memory)  →  CONTENT PACK (facts+data)  │
 └──────────────────────────────────────────────┬────────────────────────────────────────────┘
                                                │ content_pack (JSON)
 ┌──────────────────────────── LAYER 2: DECK TOOL (this code) ─▼──────────────────────────────┐
 │                                                                                            │
 │  STYLE KIT ──► [1 Intake] ─► [2 Probe]* ─► [3 Plan]† ─► [4 Compile] ─► [5 Execute]*        │
 │  (template +       │            │             │             │              │               │
 │   label sheet)  workspace   index.json    plan.json     ops.json    out.pptx+exec.json     │
 │                                                                            │               │
 │        [8 Deliver] ◄─ [7 Leak scan]* ◄─ [6 Validate]* ◄─────────────────────┘               │
 │         manifest.json   out_index.json   baseline.json, validate.json, previews            │
 │                                                                                            │
 │   * runs inside the sandbox      † the only step that calls the model                      │
 └────────────────────────────────────────────────────────────────────────────────────────────┘
```

Two processes:

| | Control plane | Data plane (sandbox) |
|---|---|---|
| What | `controlplane/orchestrator.py`, `planner.py`, `deckops/compiler.py`, `deckops/patterns.py` | `dataplane_main.py` → `deckops/probe.py`, `executor.py`, `validate.py`, `zipguard.py` |
| Opens .pptx files? | **Never** | Yes, the only place that does |
| Talks to | the model, the user | nothing (no network) |
| Hands over | file names + JSON | files + JSON reports |

---

## 3. Words used in this document

| Word | Meaning |
|---|---|
| **.pptx** | A zip file full of small XML files: one per slide, plus theme, layouts, media and a list of relationships. |
| **Shape** | Anything on a slide: text box, rectangle, circle, picture, table, chart. Each has an **id** unique on its slide (`p:cNvPr/@id`). |
| **EMU** | PowerPoint's unit of length. 914,400 EMU = 1 inch. All positions are in EMU. |
| **Example slide** | A well-designed slide in the template that new slides are copied from. |
| **Style kit** | A JSON "label sheet" for a template: which example slide is for what, and which shape id holds which content. Written once per template. |
| **Pattern** | One entry in the style kit, e.g. `chart`, `steps`, `table`. The model's menu of slide types. |
| **Content pack** | Everything the deck is allowed to use: facts and data tables, gathered by the agent. |
| **Plan** | The model's output: a list of slides, each naming a pattern and giving text and data choices. |
| **Op / step** | One exact instruction, e.g. "copy slide 260", "put this text in shape 3". The executor only understands a fixed list of these. |
| **NodeRef** | An address of something in a deck: `slide:260/shape:3/para:0/run:0`. |
| **DeckIndex** | A JSON map of a deck: every slide, shape, position and paragraph, with addresses and fingerprints. |
| **Fingerprint (`sha`)** | First 10 hex chars of the SHA-256 of a paragraph's text. Used to detect "the deck changed since the plan was made". |
| **Workspace** | One folder per run where every intermediate file lives. Production: an object-storage prefix. |

---

## 4. Files in one run (the workspace)

Every run gets `out/workspaces/<run-id>/`. Steps never call each other directly: each writes a
file, and the next step reads it. That makes every step inspectable and replayable.

| File | Size (real run) | Written by | Read by | Contains |
|---|---|---|---|---|
| `source.pptx` | 42 KB | Intake | Probe, Execute, Validate (baseline) | Copy of the template; never modified |
| `index.json` | 75 KB | Probe (sandbox) | Compile, Leak scan | DeckIndex of the template |
| `plan.json` | 6 KB | Plan (model) | Compile, Leak scan | What each slide should say |
| `ops.json` | 24 KB | Compile | Execute | Exact instructions |
| `out.pptx` | 49 KB | Execute (sandbox) | Validate, Leak scan, user | The new deck |
| `exec.json` | 1.5 KB | Execute | Deliver | OK/refused, which file parts changed |
| `baseline.json` | 38 B | Validate on `source.pptx` | Validate filter | How tight the example slides already were |
| `validate.json` | 0.3 KB | Validate on `out.pptx` | Deliver | Structural problems, overflow, preview paths |
| `render0/*.png` | — | Validate | user | One picture per slide |
| `out_index.json` | 75 KB | Probe on `out.pptx` | Leak scan | DeckIndex of the result |
| `manifest.json` | 2.4 KB | Deliver | UI / user | Summary of the whole run |

---

## 5. Step by step (create mode), following one slide

The slide we follow: **slide 5 of the new deck, "Fast enough to feel instant"**, a column chart
plus three observations, built from the template's `chart` pattern (example slide 5).

### Step 0 (once per template): build the style kit automatically

**Who:** nobody; it is code. **Code:** `orchestrator.prepare_template()` → sandbox `autokit`
(`deckops/autokit.py`) → `validate_kit` → optional vision labels (control plane) →
`kit_check.acceptance_test`.

**Input:** the template `.pptx`. **Output:** `kit.json` + `kit_report.json`.

1. *Draft (sandbox, no network).* For each slide: find chrome (title = biggest text near the top,
   subtitle under it, page number = short number at the bottom, footer = small text at the bottom);
   find repeated items (same box shape repeated 2-12 times, each copy with the same neighbours at the
   same offsets); find chart areas (big boxes that are empty or say "chart"); name the remaining text
   by capacity (only a box holding ≥ 1.8 lines can be "body", "commentary" or "intro"). Pictures seen
   on at least half the slides are decoration and are listed in `keep`; any other picture is content.
2. *Id check* (`validate_kit`): drop patterns with ids that do not exist or groups that don't line up.
3. *Vision (optional, control plane only), married to the rules.* The sandbox renders each candidate
   slide, and a copy with every fillable box outlined and numbered (`deckops/marks.py`). The control
   plane sends the numbered picture and the rules' guesses to a vision model; it answers
   `{"kind", "use_for", "offer", "boxes": {"9": "heading", ...}}`. `controlplane/kit_merge.py` merges:
   structure (boxes, groups) always from the rules; names, kind and description from vision unless a
   physical check vetoes it (box too small for long text, number not counting 1..n, value without a
   digit, page not a page number, long text marked as decoration, kind the content can't support).
   Decisions are saved per slide in `label_decisions.json`. Then the first slide of each kind is kept.
   The sandbox never has network or model access.
4. *Acceptance test.* Each pattern is filled with dummy text as long as the example's text, in a
   minimum and a full variant; one test deck is built and validated (overflow, overlap, leaked text
   or pictures). A pattern that fails is dropped and the reason goes in the report. A long-text field
   whose box holds under 1.8 lines also fails.

Measured: on the bundled example template the automatic kit matches the hand-written one exactly;
on two real templates all 9 patterns passed; two deliberately broken kits were caught.

A pattern entry looks like this:

```json
"chart": {
  "slide": 5,
  "use_for": "one chart (bar, column, stacked column, line, pie or doughnut) with 2-4 observations beside it",
  "fields": {"title": 3, "subtitle": 4, "footer": 6, "page": 7,
             "commentary_title": 10, "commentary": 11, "takeaway": 12},
  "charts": {"main": 8}
}
```
plus one style block for the whole kit:
```json
"chart_style": {"palette": ["<colour 1>", "<colour 2>", "...", "<colour 6>"],
                "font": "Arial", "text_colour": "333333", "grid_colour": "E5E7EB"}
```

Meaning: on example slide 5, shape 3 is the title, shape 11 holds the bullet points, and shape 8
is a plain rectangle marking where the chart goes. `page` and `footer` are filled automatically.
An optional `"keep": [ids]` lists pictures (logos) that stay; every other picture on the example
slide is removed when the slide is reused.

**Used by:** Step 3 (the menu shown to the model) and Step 4 (where to put each piece of content).

---

### Step 1 (agent layer): the content pack

**Who:** the chat assistant, using its tools (attachments, MCP tools, memory).
**Output:** a JSON object. This is the **only** source of facts and numbers for the deck.

```json
{
  "facts": [
    {"id": "F1", "text": "The model only fills a plan; code does layout, numbers and checks", "source": "design"}
  ],
  "datasets": {
    "stage_seconds": {
      "source": "measured: green sample deck run",
      "columns": ["Stage", "Seconds"],
      "rows": [["Read template", 0.25], ["Build", 0.29], ["Baseline check", 0.24],
               ["Check + render", 1.36], ["Leak scan", 0.27]]
    }
  }
}
```

**Used by:** Step 3 (the model reads it to decide what to say) and Step 4 (charts copy numbers
from `datasets`, never from the model).

---

### Step 2: intake + probe (sandbox)

**Code:** `orchestrator.create_from_kit()` → `Run()` → `sandbox.run(["probe", ...])` → `dataplane_main.py` → `zipguard.check()` → `probe.probe()`.

1. `Run()` creates the workspace folder and copies the template there as **`source.pptx`**.
2. The control plane starts the sandbox process. The real command:
   ```
   unshare -rn -- prlimit --as=4GB --cpu=180 --fsize=512MB --nofile=512 --
       python3 -E -s dataplane_main.py probe source.pptx index.json
   ```
   (On macOS: `sandbox-exec` with a deny-network profile plus rlimits; no memory cap.)
   `unshare -rn` gives the process no network; `prlimit` caps memory, CPU time, file size and
   open files. Its working directory is the workspace, and only file names are passed.
3. Inside the sandbox, `zipguard.check()` runs first: file size limit, entry count, total unpacked
   size, compression ratio (zip-bomb check), no `..` paths, no encryption.
4. `probe()` opens the deck with python-pptx and writes **`index.json`**. Excerpt for example slide 5:

```json
{"ref": "slide:260", "index": 4, "layout": "Blank", "shapes": [
  {"ref": "slide:260/shape:3", "kind": "text",
   "geom": {"x": 685800, "y": 347472, "w": 10972800, "h": 530352},
   "paras": [{"ref": "slide:260/shape:3/para:0", "text": "Example chart slide",
              "sha": "661c89d69f", "tagged": "<r0>Example chart slide</r0>", "runs": 1}]},
  {"ref": "slide:260/shape:8", "kind": "text",
   "geom": {"x": 548640, "y": 1417320, "w": 7223760, "h": 4663440},
   "paras": [{"text": "Chart area", "...": "..."}]},
  {"ref": "slide:260/shape:11", "kind": "text", "paras": [{"text": "Example observation one", "...": "..."}]}
]}
```
The index also lists `layouts`, `unsupported` (charts/SmartArt/OLE it can't edit) and
`external_rels` (links to outside the file).

**Output → used by:** `index.json` → Step 4 needs the slide's address (`slide:260`), every shape's
geometry (to re-flow grids) and every shape's text (for the leak guard). The control plane never
opens the .pptx itself; it only reads this JSON.

---

### Step 3: plan (the model, the only AI step)

**Now done in two stages** (`controlplane/slide_flow.py`, `create_from_kit(two_stage=True)`):
1. `planner.outline(brief, content, max_points)` → `outline.json`: per slide its purpose, kind
   (numbers, sequence, comparison, list, table, chart, text), title, points with the fact each comes
   from. No layout.
2. For each slide, `options_for()` lists the layouts that can hold it (measured item limits, table/chart
   match, cover first, closing last); `planner.render_slide(content, options, context)` picks one and
   fills it. Run in parallel.
3. Each answer is compiled alone; a `CompileError` goes back with the previous answer (`feedback`), up to
   2 retries. After building, overflow findings go back as "rewrite it in at most N characters", where
   N comes from the lines the box holds, and only those slides are redone (`repairs.json`).

The original one-call planning below is still available (`--single-call`).

**Code (one call):** `planner.plan_from_kit(brief, describe_kit(kit), content_pack)`.

**Input to the model:**
- A system prompt (`KIT_SYSTEM` in `planner.py`): use only these patterns, use only facts from
  the content pack, never write positions, colours or numbers into charts, keep text short.
- The menu, produced by `describe_kit(kit)` from the style kit. Real text:
  ```
  - chart: one chart (bar, column, stacked column, line, pie or doughnut) with 2-4 observations
    beside it. fields: ['title', 'subtitle', 'commentary_title', 'commentary', 'takeaway'];
    charts: ['main'] (each: type, dataset, category column, series columns)
  - steps: a numbered process of 3-8 steps ... items: 3-8 of ['number', 'heading', 'body']
  ...
  Chart types: bar, column, stacked_column, line, pie, doughnut.
  ```
  Notice what's missing: no shape ids, no positions, no colours.
- The user's request and the content pack.

**Output: `plan.json`.** The entry for our slide (real):
```json
{"pattern": "chart",
 "fields": {"title": "Fast enough to feel instant",
            "subtitle": "Seconds per sandbox stage for one 7-slide deck",
            "commentary_title": "What it shows",
            "commentary": ["About 2.4 seconds in total",
                           "Rendering slide previews is the slowest stage",
                           "Model time is not included: no model was called here"],
            "takeaway": "Measured in this prototype, before any tuning."},
 "charts": {"main": {"type": "column", "dataset": "stage_seconds", "category": "Stage",
                     "series": ["Seconds"], "number_format": "0.00"}}}
```
The model chose the pattern, the words, the chart type and **which dataset and columns**. It did
not write any of the five numbers.

**Used by:** Step 4. (In this prototype the plan is written by hand because the sandbox has no
model access; with a model configured, `planner.OpenAICompatPlanner` produces it.)

---

### Step 4: compile (plain code, pure function)

**Code:** `patterns.compile_from_kit(plan, index, kit, content_pack)` → list of ops → `ops.json`.
No files, no network, no model: same inputs always give the same output, so it's unit-tested.

For each slide in the plan:

1. **Look up the pattern** in the kit (`chart` → example slide 5) and the example slide in
   `index.json` (`slide:260`). Unknown pattern → error listing the available ones.
2. **Fields → text steps.** For every field in the kit: take the value from the plan (or the
   automatic value for `page` = `"5 / 8"` and `footer`). Value present → `set_shape_text`;
   missing → `delete_shape`. Lists become one paragraph per item. Unknown field names → error.
3. **Charts → chart step** (`compile_chart`). Finds dataset `stage_seconds` in the content pack,
   checks columns `Stage` and `Seconds` exist and all values are numbers (pies: one series, no
   negatives, ≤ 8 slices), then **copies the numbers from the dataset** into a `set_chart`
   step, together with the palette and font from `chart_style`.
4. **Repeated items** (for patterns like `steps` or `columns`; not used on this slide):
   `layout_items()` computes a grid inside the area the example items used. Number of rows =
   ceil(n / max_columns), columns = ceil(n / rows). Item width = (area width − gaps) / columns.
   For each shape in an item: keep its offset inside the item; stretch it if it reaches the item's
   far edge, otherwise keep its size (so circles stay round). More items than the example → 
   `duplicate_shape` with new ids; fewer → `delete_shape` for the extras.
5. **Tables** (pattern `table`): column count must match the example; one `set_table` step.
6. **Leak guard:** every shape on the example slide that still holds text and was **not** claimed
   by a field, item, chart or table gets a `delete_shape`. Nothing from the example can survive.
7. Emit one `add_slide_from_pattern` op with all the steps.

After all slides: one `delete_slide` per original example slide, and one `set_doc_properties` (new
title, empty author/comments) so the template's file properties don't travel with the new deck.

**Output: `ops.json`** (18 ops for 8 slides). The op for our slide (real):
```json
{"op": "add_slide_from_pattern", "source": "slide:260", "steps": [
  {"op": "set_shape_text", "target": "shape:3",  "paragraphs": ["Fast enough to feel instant"]},
  {"op": "set_shape_text", "target": "shape:4",  "paragraphs": ["Seconds per sandbox stage for one 7-slide deck"]},
  {"op": "set_shape_text", "target": "shape:6",  "paragraphs": ["<footer text from the plan>"]},
  {"op": "set_shape_text", "target": "shape:7",  "paragraphs": ["5 / 8"]},
  {"op": "set_shape_text", "target": "shape:10", "paragraphs": ["What it shows"]},
  {"op": "set_shape_text", "target": "shape:11", "paragraphs": ["About 2.4 seconds in total",
        "Rendering slide previews is the slowest stage", "Model time is not included: no model was called here"]},
  {"op": "set_shape_text", "target": "shape:12", "paragraphs": ["Measured in this prototype, before any tuning."]},
  {"op": "set_chart", "target": "shape:8", "chart_type": "column",
   "categories": ["Read template", "Build", "Baseline check", "Check + render", "Leak scan"],
   "series": [{"name": "Seconds", "values": [0.25, 0.29, 0.24, 1.36, 0.27]}],
   "number_format": "0.00", "show_values": true,
   "palette": ["<colour 1>", "<colour 2>", "..."], "font": "Arial", "text_colour": "333333", "grid_colour": "E5E7EB"}
]}
```
Any error in any slide → `CompileError` with the full list of problems, and nothing is built.
In production those messages go back to the model to fix its plan.

**Used by:** Step 5.

---

### Step 5: execute (sandbox)

**Code:** `sandbox.run(["execute", "source.pptx", "ops.json", "out.pptx", "exec.json"])` →
`executor.execute()`.

1. **Schema check.** `OpList.validate_python(ops)`: every op must be one of the known pydantic
   models (`deckops/ops.py`) with the right fields and types. Unknown op or field → refused.
2. **Bind (check everything before changing anything).** For each op, resolve every address
   against `source.pptx`:
   - the source slide exists and holds no chart/SmartArt/OLE (those can't be copied safely yet);
   - every step's target shape id exists on that slide, or was created by an earlier
     `duplicate_shape` in the same op;
   - for edit ops on existing decks: the paragraph's current fingerprint equals the op's `expect`.
     If anything fails, **the whole batch is refused**: `exec.json` = `{"ok": false, "errors": [...]}`,
     exit code 3, no output file.
3. **Apply, in phases:** text edits → font/geometry → shape deletes → new slides → slide deletes
   → file properties. Within `add_slide_from_pattern`:
   - **`clone_slide`:** add a slide on the same layout, remove its default placeholders, deep-copy
     every shape from the example slide, copy its background, and re-link pictures (each copied
     `r:embed`/`r:id` is remapped to a new relationship pointing at the same image).
   - **`set_shape_text`:** removes the shape's paragraphs and rebuilds them, each copying the
     formatting of the example paragraph in the same position. Real XML for shape 3:
     ```xml
     before: <a:r><a:rPr sz="2600" b="1"><a:solidFill><a:srgbClr val="1A1A1A"/></a:solidFill>
                   <a:latin typeface="Arial"/></a:rPr><a:t>Example chart slide</a:t></a:r>
     after:  <a:r><a:rPr sz="2600" b="1"><a:solidFill><a:srgbClr val="1A1A1A"/></a:solidFill>
                   <a:latin typeface="Arial"/></a:rPr><a:t>Fast enough to feel instant</a:t></a:r>
     ```
     Only the text inside `<a:t>` changed; size, bold, colour and font came from the example.
   - **`set_chart`:** reads the rectangle's position (`x=548640 y=1417320 cx=7223760
     cy=4663440`), deletes the rectangle, and adds a real PowerPoint chart in exactly that spot.
     python-pptx writes `ppt/charts/chart1.xml` plus an embedded Excel workbook
     (`ppt/embeddings/Microsoft_Excel_Sheet1.xlsx`) holding the numbers. Then it applies the
     palette, font, light grid lines, data labels and number format.
4. **Save, keeping original bytes** (`save_preserving`). python-pptx rewrites every file it read,
   even unchanged ones. So after saving, each part is compared with the original *by meaning*
   (canonical XML; relationships with resolved targets; content types of parts that exist). Any
   part with the same meaning gets its **original bytes** back.

**Output: `out.pptx` + `exec.json`** (real, shortened):
```json
{"ok": true, "ops_applied": 18, "warnings": [],
 "parts": {"preserved": 32,
           "changed": ["[Content_Types].xml", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels", "docProps/core.xml"],
           "added": ["ppt/slides/slide10.xml", "...", "ppt/charts/chart1.xml", "ppt/embeddings/Microsoft_Excel_Sheet1.xlsx", "..."],
           "removed": ["ppt/slides/slide1.xml", "..."]}}
```
`removed` lists the example slides; `added`, the new ones and the chart. For edit mode this is how
"133 of 134 parts unchanged" is proven.

**Used by:** Step 6 reads `out.pptx`; Step 8 copies `exec.json` into the manifest.

---

### Step 6: validate (sandbox, twice)

**Code:** `sandbox.run(["validate", ...])` → `validate.validate()`.

1. **Baseline, on `source.pptx`** (example slides only, no rendering) → `baseline.json`.
   It records how tight the example slides already are. The overflow estimate is approximate, and
   many real templates are already near the limit.
2. **On `out.pptx`** (all slides, with rendering) → `validate.json`:
   - **Structural checks:** every part has a content type; no duplicate shape ids on a slide.
   - **Overflow prediction**, per text shape: find the effective font size (run → shape style →
     layout → master), the font (mapped to a metric-identical substitute: Calibri → Carlito,
     Arial → Liberation Sans), wrap the words to the box width with real glyph widths, and
     compare the needed height with the box height. Also notes whether the template relies on
     PowerPoint's autofit.
   - **Render:** LibreOffice headless → PDF → `pdftoppm` → one PNG per slide in `render0/`.
3. The control plane keeps only overflow findings that are **worse than the baseline** for the same
   example shape (ratio > 1.03 × max(1, baseline ratio)).

Real output: `{"structural": [], "findings": [], "pdf": "render0/out.pdf", "previews": ["render0/slide-01.png", ...]}`

**Used by:** Step 8. In translate mode, findings trigger repair (section 6).

---

### Step 7: leak scan (sandbox + control plane)

**Code:** `sandbox.run(["probe", "out.pptx", "out_index.json"])`, then `patterns.leaked_text()`.

Collects every paragraph of 12+ characters from the example slides (`index.json`), removes any
text the plan itself supplied (the model may legitimately reuse "What it shows"), and intersects
the rest with every paragraph in the new deck (`out_index.json`). It does the same for pictures,
by a fingerprint (SHA-256) of the image bytes, skipping pictures the kit keeps. Anything left =
leaked template content. Real result: `[]`.

**Used by:** Step 8, and a non-empty result should block delivery.

---

### Step 8: deliver

`manifest.json` gathers it all: `run`, `mode`, `kit`, `planner`, `slides`, `ops`, `exec` (part
statistics), `findings`, `structural`, `leaked_example_text`, `previews`, and a `trace` of every
sandbox call with exit code and seconds:

```json
"trace": [{"stage": "probe", "rc": 0, "seconds": 0.2}, {"stage": "execute", "rc": 0, "seconds": 0.2},
          {"stage": "validate", "rc": 0, "seconds": 0.2}, {"stage": "validate", "rc": 0, "seconds": 1.8},
          {"stage": "probe", "rc": 0, "seconds": 0.2}]
```
The user receives `out.pptx`, the slide pictures and the change list.

---

## 6. The other modes use the same machinery

| | Translate | Edit (e.g. 8 cards → 6) | Create from template (above) |
|---|---|---|---|
| Starts from | user's deck | user's deck | template deck |
| Model writes | each paragraph translated, keeping tags `<r0>…</r0>` (formatting runs) | which cards merge + new text | pattern + text + chart choices |
| Compiler | `compile_translate`: one `set_paragraph_text` per changed paragraph, with the original fingerprint | `compile_card_grid`: detects circle/number/heading/body cards; `set_geometry`, `set_paragraph_text`, `delete_shape` | `compile_from_kit` |
| Special checks | tags must exist; fields kept | cards must exist | patterns, fields, data |
| After validate | **repair loop**: if text overflows, ask the model to shorten those paragraphs, recompile, re-execute **from source**; second failure → `set_font_size` fallback; max 2 rounds | — | — |
| Guarantee | untouched slides byte-identical | only the edited slide changes | no example content left |

Why translation works per paragraph: word order changes between languages
("Revenue grew **12%**" → "**+12 %** de croissance"). The model receives `<r0>Revenue grew </r0><r1>12%</r1>`
and may reorder the tags; each tag keeps its own formatting.

---

## 7. Data contracts

### 7.1 Content pack (agent → deck tool)
```json
{"facts":    [{"id": "F1", "text": "...", "source": "..."}],
 "datasets": {"<id>": {"source": "...", "columns": ["Category", "Value 1", "..."], "rows": [["A", 1.0], "..."]}}}
```

### 7.2 Style kit (per template)
```json
{"name": "...", "template": "template.pptx", "page_format": "{n} / {total}",
 "chart_style": {"palette": ["RRGGBB", "..."], "font": "...", "text_colour": "RRGGBB", "grid_colour": "RRGGBB"},
 "patterns": {"<name>": {
    "slide": 5, "use_for": "...",
    "fields": {"<field>": <shape id>},
    "items":  {"groups": [[<ids of item 1>], [<ids of item 2>]], "fields": {"<field>": <position in group>},
               "min": 1, "max": 8, "max_columns": 4},
    "charts": {"<slot>": <shape id of chart-area rectangle>},
    "table":  {"shape": <shape id>, "max_rows": 9}}}}
```

### 7.3 Plan (model → compiler)
```json
{"footer": "...", "title": "...(optional)",
 "slides": [{"pattern": "<name>", "fields": {"<field>": "text" | ["bullet", "..."]},
             "items": [{"<field>": "..."}], "table": {"header": ["..."], "rows": [["..."]]},
             "charts": {"<slot>": {"type": "bar|column|stacked_column|line|pie|doughnut", "dataset": "<id>",
                                   "category": "<column>", "series": ["<column>"], "title": "", "number_format": "0.0"}},
             "notes": "speaker notes (optional)"}]}
```

### 7.4 Ops (compiler → executor), `deckops/ops.py`

| Op | Purpose |
|---|---|
| `set_paragraph_text` | Rewrite a paragraph from tagged runs (translate/edit); carries `expect` fingerprint |
| `set_run_text` | Replace one run's text |
| `set_font_size` | Explicit size for all runs of a shape (overflow fallback) |
| `set_geometry` | Move/resize a shape (EMU); compiler-generated only |
| `delete_shape` | Remove a shape |
| `add_slide` | New slide from a placeholder layout, slots filled by index |
| `add_slide_from_pattern` | Copy an example slide, then run its `steps` on the copy |
| ↳ step `duplicate_shape` | Copy a shape under a new id (more items than the example) |
| ↳ step `set_shape_text` | Replace all text, paragraph k formatted like example paragraph k |
| ↳ step `set_table` | Header + rows, styled like the example rows (banding kept) |
| ↳ step `set_chart` | Replace chart-area rectangle with a real chart from given numbers |
| `delete_slide` | Remove a slide |
| `set_doc_properties` | Reset title/author/comments/dates |

### 7.5 Reports (sandbox → control plane)
`index.json` (DeckIndex), `exec.json` (`ok`, `errors` or `parts`), `validate.json` (`structural`,
`findings`, `previews`). Every sandbox command writes a JSON report even when it fails: `rejected`
(unsafe archive, exit 4) or `crash` (traceback, exit 5).

---

## 8. Sandbox and security

| Threat | Control |
|---|---|
| Zip bomb / path traversal | `zipguard.check()` before any parser: size, entry count, ratio, paths, encryption |
| Malicious XML | lxml parser with entity resolution and network access off |
| Exploit in LibreOffice or parsers | Runs in the sandbox: no network (`unshare -n`), memory/CPU/file caps (`prlimit`), clean env, timeout |
| Deck fetches remote content | No network inside; probe lists external links |
| Model invents numbers | Charts copy values from datasets; model only names them |
| Model asks for impossible edits | Compile validates against the index; bind validates against the file |
| Concurrent change to the deck | Fingerprints on edit ops; whole batch refused if any mismatch |
| Template content leaks | Unfilled text deleted, output scanned, file properties reset |

**Prototype limit:** the namespace sandbox has no network but can still *read* the host
filesystem and shares the host kernel. Production: replace `controlplane/sandbox.run()` with a
microVM runtime (e.g. Kata Containers / Firecracker) and the workspace folder with object storage.
The command-line + JSON contract stays the same.

---

## 9. Code map

| Module | Runs in | Lines (approx.) | Role |
|---|---|---|---|
| `controlplane/orchestrator.py` | control | 215 | Pipelines: `translate`, `create`, `edit`, `create_from_kit`; workspace; repair loop |
| `controlplane/planner.py` | control | 160 | Model prompts; offline stub; OpenAI-compatible client (config file, no env vars) |
| `controlplane/sandbox.py` | control | 55 | Launch a data-plane command isolated; **swap point for a microVM** |
| `deckops/compiler.py` | control | 210 | `compile_translate`, `compile_create`, `compile_card_grid` |
| `deckops/patterns.py` | control | 330 | Style kits, `compile_from_kit`, grid layout, charts from data, leak check |
| `deckops/refs.py` | both | 210 | NodeRef grammar, fingerprints, tagged-run text |
| `deckops/ops.py` | both | 165 | The op language (pydantic models) |
| `dataplane_main.py` | sandbox | 65 | Only entry point that reads deck bytes |
| `deckops/zipguard.py` | sandbox | 40 | Archive safety |
| `deckops/probe.py` | sandbox | 155 | .pptx → DeckIndex |
| `deckops/executor.py` | sandbox | 505 | Bind, apply, clone slides, draw charts, byte-preserving save |
| `deckops/validate.py` | sandbox | 190 | Structure, overflow prediction, rendering |
| `deckops/autokit.py` | sandbox | 390 | Draft a style kit from a template with rules |
| `controlplane/kit_check.py` | control | 200 | Acceptance test for a kit; drops failing patterns |
| `controlplane/slide_flow.py` | control | 170 | Two-stage generation: options per slide, split, per-slide retry |
| `controlplane/kit_merge.py` | control | 150 | Merge rule labels with vision labels, with physical vetoes |
| `deckops/marks.py` | sandbox | 45 | Draw numbered box outlines on slide pictures for the vision model |
| `tools/inspect_deck.py` | local | 30 | Shape ids per slide (debugging) |
| `tools/compare_kits.py` | local | 55 | Automatic kit vs a reference kit |
| `samples/`, `examples/`, `tests/` | — | — | Example templates and kits, runnable examples, 26 tests |

---

## 10. Known limits and next steps

1. **Connect a real model** through the existing OpenAI-compatible planner. Two-stage generation,
   per-slide retries and the overflow repair loop are built and tested with offline renderers, but
   not yet against a real model.
2. **Headline numbers should be data-bound like charts.** Today `stats` values are text typed by
   the model; they should reference a dataset cell.
3. **Overlap detection** covers text shapes only, not pictures or decoration.
4. **Patterns can't contain charts or SmartArt** (use a chart-area rectangle instead).
5. **Restyle mode** (move an existing deck onto a new template) is not built yet.
6. **Production sandbox and storage:** microVM per editing session, workspace in object storage,
   checkpointing so a run survives a restarted worker.
7. **Orchestration:** wrap the steps as nodes of the host application's workflow framework, with
   persisted state; one progress event per sandbox call.
8. **Automatic kit rules** were tuned on two templates; more templates will need more rules. Failing
   patterns are dropped, not guessed. The vision step is wired but untested against a real model.
9. **Item limits are measured** (`kit_check.measure_capacity`): test slides with more items than the
   example, text 15% longer than the example's, up to 16; the limit is the last count with no overflow
   and no item leaving the example's item area. Real text much longer than the example's can still
   overflow; the repair loop handles that.
