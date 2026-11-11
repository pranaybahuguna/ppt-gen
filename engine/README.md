# engine/ — the deckops engine, vendored

`engine/deckops/` is a clone of <https://github.com/pranaybahuguna/deckops>, used **unmodified**.

    git clone https://github.com/pranaybahuguna/deckops engine/deckops

It is cloned, and then **patched** — see `patches/README.md`. Up to group support it was used
entirely unmodified, and everything else still is. Every change this project needs is made by *composition*:

| What we needed | How we get it without touching the engine |
|---|---|
| Run the data plane in Docker instead of `unshare`/`sandbox-exec` | `deck_agent.engine_bridge.install_docker_sandbox()` replaces the single function `controlplane.sandbox.run`, whose contract (argv in, files + JSON report out) the engine's own docstring says is the production seam |
| Decompose the pipelines into graph nodes | nodes call the engine's *pieces* (`deckops.probe`, `deckops.compiler`, `deckops.patterns`, `controlplane.slide_flow`) rather than `controlplane.orchestrator.translate()` wholesale |
| Keep artefacts in object storage | nodes stage files into a temp workspace and publish them to S3; the engine still sees plain local paths |

`pytest engine/deckops/tests` (28 tests) stays a valid regression check either way, and it
passes with the patches applied — which is what makes patching the engine safe to do at all.
`make engine-reset` returns the clone to upstream.

The one thing composition could not reach is group support: the rules deciding whether a slide
can become a reusable layout live inside the engine and read the file themselves, so nothing
outside can change their minds. That is `patches/0001-group-support.patch`, and it took a real
commercial template from 0 usable layouts to 5.

To make this a real dependency later: `git submodule add` it, or publish the engine as a wheel and
drop `engine/` entirely — `engine_bridge.py` is the only file that knows where it lives.
