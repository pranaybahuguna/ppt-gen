# engine/ — the deckops engine

`engine/deckops/` is a clone of <https://github.com/pranaybahuguna/deckops>, **patched** by this
project. It is git-ignored here; `make engine` clones it and applies `patches/*.patch`:

    make engine          # clone + patch (idempotent)
    make engine-reset    # drop the patches, back to upstream
    make test-engine     # the engine's own 28 tests, which pass with the patches applied

**How the agent uses it.** In-process, as a library. `src/deck_agent/engine/__init__.py`
(`bootstrap()`) puts the clone on `sys.path`; the graph's nodes call the engine's pieces -
`deckops.probe`, `deckops.compiler`, `deckops.patterns`, `deckops.executor`,
`controlplane.slide_flow`, `controlplane.kit_check` - rather than its end-to-end orchestrators,
so every step is a node with its own state, trace and checkpoint. Files are staged into a local
workspace and published to object storage; the engine only ever sees local paths.
`docs/01-why-one-process.md` explains why there is no separate engine process.

**What the patch changes**, and why composition could not: the rules deciding whether a slide
can become a reusable layout live inside the engine and read the file themselves. The patch
gives the engine group support (slide-space geometry for shapes inside groups, writing it back,
checks that look inside groups), a better rule for which pictures are decoration, and
placeholder text that is at most the example's length. On a real commercial template it took
the kit from 0 usable layouts to 6. `patches/README.md` has the details, file by file.

**Pinned to.** The patch is a `git diff` against the upstream commit in use when it was made
(`make doctor` prints the engine commit). If upstream moves and the patch no longer applies,
`make engine` says so and stops; redo the change against the new upstream rather than forcing
it.

To make this a real dependency later: publish the engine as a wheel with the patch upstreamed,
and drop `engine/` - `src/deck_agent/engine/__init__.py` is the only file that knows where it
lives.
