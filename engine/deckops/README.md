# engine/deckops — the PowerPoint engine

The part of this project that reads and writes `.pptx` files. The model never writes a deck: it
writes a **plan** (what each slide says, and which of the template's layouts holds it). A pure
**compiler** turns the plan into typed **operations**, and the **executor** applies them.
Untouched slides come out byte-identical, every change is an auditable operation, and the model
never produces coordinates or XML.

| Package | What is in it |
|---|---|
| `deckops/` | probe (read a deck), compiler and patterns (plan → operations), executor (apply them, byte-preserving save), autokit (find a template's reusable layouts), validate (overflow and overlap checks), refs (shape addressing, including shapes inside groups) |
| `controlplane/` | the model client (`planner`), two-stage slide planning (`slide_flow`), helpers for testing a kit (`kit_check`) and for merging rule and vision labels (`kit_merge`) |
| `samples/` | scripts that build the sample deck and template the tests and the demo use (`make samples`) |
| `tests/` | the engine's own tests (`make test-engine`); `tools/compare_kits.py` is used by them |

The agent in `src/deck_agent/` uses it in-process as a library: `src/deck_agent/engine/__init__.py`
puts this directory on the import path, and the graph's nodes call its pieces directly. Nothing
here needs installing separately - `make venv` installs `requirements.txt` from this directory
along with the agent's.
