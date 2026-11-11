# 1. Why one process, and where the container actually earns its place

This document explains the central design decision, because everything else follows from it.

## What deckops does today

The engine splits itself in two. A **control plane** orchestrates and talks to models but never
opens a `.pptx`; a **data plane** does all the file work and runs inside a sandbox with no
network. They talk through a subprocess boundary: argv in, files and a JSON report out.

```
control plane  ──► sandbox.run(["probe", "in.pptx", "index.json"])  ──►  data plane
               ◄──  index.json                                      ◄──
```

The sandbox was the **trust boundary**: a `.pptx` is an untrusted zip of XML, so it was parsed
somewhere it could do no harm.

## What that boundary is worth here

Take the operations one at a time and ask what the sandbox is protecting against.

| Operation | What it actually does | Native code? | Network? |
|---|---|---|---|
| `probe` | read the zip, walk the XML with `lxml`, emit JSON | python-pptx + lxml | no |
| `execute` | bind, apply typed ops, save part by part | python-pptx + lxml | no |
| checks | measure text with font metrics, compare rectangles | Pillow + python-pptx | no |
| `autokit` | read geometry, propose patterns | python-pptx | no |
| **render** | **`soffice` → PDF, `pdftoppm` → PNG** | **LibreOffice, poppler** | **wants to** |

Four of the five are pure Python over two libraries. They cannot open a socket or start a
process, because nothing in them does either. The genuine risks of parsing a hostile `.pptx`
are a zip bomb and an XML entity attack, and the engine already answers the first directly:
`deckops/zipguard.py`, 38 lines, checks entry count, uncompressed size, compression ratio,
encrypted entries and path traversal *before any parser sees the file*. That check is not
specific to a sandbox; it works just as well at the front door.

The fifth is different in kind. LibreOffice is a few hundred megabytes of C++ with a long CVE
history, and it will cheerfully fetch remote content referenced from a document it is asked to
convert. That one *is* worth putting behind a wall.

So the boundary was in the wrong place: it was drawn around "anything that touches a deck",
when the thing that actually needs containing is "the native toolchain, and anything a model
wrote".

## What the split cost

- **A subprocess per step.** Probe, execute, validate, autokit - each one a process start, a
  workspace, JSON serialised to disk and read back.
- **An argv-and-files protocol** between two halves of the same program, with its own error
  codes, report files and failure modes, all of which had to be designed, documented and
  tested.
- **Pipelines that cannot be decomposed.** `orchestrator.create_from_kit` is one long function
  because every step has to marshal through that boundary. Cutting it into graph nodes meant
  re-plumbing it.
- **An incomplete boundary anyway.** The engine's own `sandbox.py` says so: there is no mount
  namespace, so the sandboxed process can still read the whole host filesystem. It stopped
  network access and capped memory; it did not stop a parser reading `~/.ssh`.

## What this project does instead

**One process** does all the deterministic work, calling the engine's functions directly:

```python
index  = deck.probe(path)                     # no subprocess
report = deck.apply_ops(src, ops, dst)        # no subprocess
checks = deck.check(dst)                      # no subprocess
```

`zipguard` moves to intake, where it runs on anything arriving from outside. The whole
deterministic path - probe, compile, apply, byte-preserving save, overflow, overlap, structural
checks, kit building, capacity measurement - is in-process Python with no external process at
all.

**One container image** is kept, for two jobs that are not the same kind of thing:

| Job | Why it is in a container | Switch |
|---|---|---|
| **Code a model wrote** | A **trust boundary**. Nobody reviews that code before it runs. No network, read-only root filesystem, no capabilities, 512 MB, seconds of CPU, destroyed after one call. | `code_runner` |
| **Rendering previews** | A **dependency boundary**. LibreOffice, poppler and the metric-compatible fonts are a ~1 GB native toolchain; keeping them in an image means the agent needs nothing installed. That it is also the component that wants the network is a bonus. | `render_backend` |

The second is honestly a packaging decision, not a security one, and it is switchable: set
`render_backend=local` and a locally installed `soffice` is used instead, leaving the container
used only for job one. Set `render_backend=off` and decks are still built and checked - text
fitting is predicted from font metrics, not from pictures - you simply get no previews, and
the result says so rather than quietly returning nothing.

## What is given up, stated plainly

A hostile `.pptx` is now parsed in the agent's own process. That is a real change, and these
are the mitigations, in the order they apply:

1. **`zipguard` at intake** - zip bombs, encrypted entries, path traversal and absurd sizes are
   refused before a parser is involved. Tested in `tests/unit/test_engine.py`.
2. **The parsers are the same ones** that were being used before. Moving the call site does not
   make `lxml` more fragile.
3. **The blast radius is a request, not a host.** In production the agent is a stateless worker;
   a crash takes down one request.

What is genuinely weaker: a parser bug that achieved code execution would now run with the
agent's own privileges, including its object-storage credentials, rather than in a network-less
namespace. If that matters for a given deployment - decks arriving from outside the
organisation, say - the honest answer is not to re-create the old split but to run the whole
agent as the untrusted thing: one short-lived worker per request, with credentials scoped to
that request's prefix. `docs/07-operations.md` says how.

For the stated setting - an internal chat platform where the person uploading a deck is an
authenticated employee - the trade is worth taking, and the thing that was always worth
containing is still contained.

## The engine is not modified

`engine/deckops` is a clone, used as-is. Its own 28 tests still pass (`make test-engine`), so
it remains a valid regression check. Everything this design needed was achieved by composition:
calling its functions rather than its subprocess entry point, and adding small named pieces
next to it (`engine/fallback.py`, `engine/tidy.py`, `engine/edits.py`). See `engine/README.md`.
