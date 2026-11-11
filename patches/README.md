# patches/ — changes to the deckops engine

The engine is a clone, kept out of this repository (see `engine/README.md`). It was first used
**unmodified**, with every change this project needed made by composition.
Group support could not be: the rules that decide whether a slide can be a reusable layout live
inside the engine and read the file themselves, so nothing outside it can change their minds.

So the engine is now patched, and the patches live here rather than in a forked clone:

* `make engine` clones upstream and applies them;
* `make engine-patch` applies them to an existing clone (idempotent — it says "already
  applied" rather than failing);
* `make engine-reset` drops them and returns the clone to upstream;
* `make test-engine` still runs the engine's own 28 tests, which **pass with the patches
  applied** — that is the regression check that makes this safe to do.

Each patch is a plain `git diff` against the upstream commit named in its header. A patch that
no longer applies means the engine has moved underneath it, and the fix is to redo the change
against the new upstream rather than to force it.

---

## 0001-group-support.patch

**What it is for.** Most commercial templates build their content slides out of grouped
shapes — an icon, a heading and a body, grouped into a card, repeated across the slide. The
engine skipped any slide holding a group, so on the first real bought template tried, nineteen
of its twenty slides were refused and the kit came out empty. This patch makes those slides
usable. On that template it took the result from **0 usable layouts to 5**.

**Why the engine was right to refuse before.** A shape inside a group states its position in
that group's *child coordinate space*, not the slide's:

    group   off=(11388726, 591138)   ext=(360362, 272462)
    group   chOff=(8953500, 329200)  chExt=(276225, 166687)
    child   x=8953500  y=329200             <- the group's top-left, in child units

Finding patterns is entirely geometric — which boxes repeat, at what spacing, with which
neighbours — so reading those numbers as slide coordinates would produce nonsense. Refusing
beat guessing.

**What the patch changes**, in five places:

| File | Change |
|---|---|
| `refs.py` | `group_transform`, `to_slide`, `to_own`: the exact mapping between a shape's own space and the slide's, composed through nested groups |
| `probe.py` | `_geom` reports slide coordinates, so everything downstream compares like with like |
| `autokit.py` | `leaves()` opens groups up, `S` converts geometry, and a slide with groups is no longer skipped (a slide with a real chart still is) |
| `executor.py` | `set_geometry` converts back into the shape's own space before writing; a shape duplicated *out of* a group has its geometry rewritten into slide space, because the copy is appended to the slide |
| `autokit.py` | what counts as *decoration* - a picture that comes with the layout rather than content on an example slide |
| `kit_check.py` | placeholder text is *at most* the example's length, not a word longer |
| `validate.py` | the overflow and overlap checks open groups up and measure each box at the size it is drawn |
| `patterns.py` | a picture the pattern itself uses is not a leak — with groups usable, an item is routinely "icon + heading + body" and the icon is meant to be copied once per item |

**Which pictures a layout keeps.** The original test was frequency alone: a picture on at
least half the slides is decoration, everything else is content and is stripped from the copy.
That is right about a logo repeated throughout and wrong about everything that appears once by
design. On this template it stripped the cover photograph, the logo, an icon inside a circle
and a row of tick marks, and the acceptance test's vision check named each one:

> *"The large foreground photo of the person is missing, substantially changing the
> composition."* · *"The four green checkmark icons above the lower text columns are missing."*

Two more signals, both about what a picture *is* rather than how often it appears: it is
**small** (under 0.8 inch in either direction - the logos and icons here measure 0.34-0.77
inch, the content photographs 3-6 inch), or it **bleeds** off the edge of the slide. What is
still removed is a picture of real size, inside the page, appearing once - the screenshot or
diagram the rule was written for, and the one in the engine's own leak test, which still fails
the way it should. On this template that took acceptance from 3 patterns to 5.

**Placeholder text that answers the question actually asked.** The acceptance test asks
whether a pattern holds content like its example's, and built that content with `dummy()`,
which appended whole words until it *passed* the requested length - overshooting by up to a
word. On long body text that is nothing; on a short string it is a third more. A 12-character
title came back as 16, which tipped a one-line heading box onto a second line, and the layout
was dropped for an overflow it does not have. `dummy()` now stops before exceeding the length.
The capacity measurement still asks for 15% more on purpose, by passing `stretch`, so that
stays deliberate rather than accidental. This took acceptance from 5 patterns to 6.

**Checks that can see inside groups.** Once text could live inside a group, the overflow and
overlap checks had to look there too - and they did not: both walked only the shapes at the top
of the slide. On a deck built from this template, a 105-character sentence placed in a two-word
label box inside a group needed 6.4 times the room it had, and was reported as fine; the slide
went out with its text running down the page. They now open groups up, and measure each box at
the size the group draws it (a group scales its children's boxes, though not their font sizes).

**One judgement call worth review.** The same patch stops an *embedded object* from blocking a
slide being copied. `_NO_CLONE` listed charts, SmartArt and OLE together; `clone_slide` shares
a referenced part rather than duplicating it, which is wrong for a chart (editing one would
edit both) but ordinary for an embedded object. Commercial decks are routinely built with
add-ins — think-cell above all — that park an inert data blob on **every** slide, so treating
that like a chart meant refusing entire templates. Charts and SmartArt still block copying.
The object itself is still never edited, and `compile_from_kit` now drops it from the copy so
a new deck does not inherit somebody else's add-in data.

**What it does not do.** Slides holding a real chart are still skipped, and the three such
slides in that template are still refused. Nested groups are handled by composing the
transform, but a group that is rotated or flipped is not something this patch reasons about —
its children are read with the group's scale and offset only.
