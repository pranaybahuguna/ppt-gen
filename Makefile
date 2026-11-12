# deckops-agent. `make help` lists everything.
SHELL := /bin/bash
PY    := .venv/bin/python
PIP   := .venv/bin/pip
COMPOSE := docker compose -f docker/compose.yml

export PYTHONPATH := $(CURDIR)/src

.PHONY: help
help:  ## list the targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup

.PHONY: setup
setup: venv config image fonts samples  ## everything needed to run, from a fresh clone
	@echo; echo "now: make up && make init && make doctor   (README.md has the rest)"

.PHONY: venv
venv:  ## create the virtualenv and install dependencies
	@test -d .venv || python3.12 -m venv .venv
	@$(PIP) install -q --upgrade pip
	@$(PIP) install -q -r requirements-dev.txt -r engine/deckops/requirements.txt
	@$(PIP) install -q --no-deps -e .          # the project itself, so `deckops` is a command
	@$(PY) --version

.PHONY: config
config:  ## write config/agent.json from the example, if there is none (never overwrites)
	@test -f config/agent.json && echo "config/agent.json exists, left alone" || \
		(cp config/agent.example.json config/agent.json && echo "wrote config/agent.json")
	@test -f llm.json || echo "no llm.json yet: copy llm.example.json and fill it in (README.md)"

.PHONY: image
image:  ## build the sandbox image (python + LibreOffice + poppler + fonts)
	docker build -f docker/sandbox.Dockerfile -t deckops/sandbox:0.1.0 .

.PHONY: fonts
fonts:  ## copy the metric-compatible fonts out of the image, so the agent needs none installed
	@mkdir -p local/fonts
	@docker run --rm --network none --user $$(id -u):$$(id -g) -v "$(CURDIR)/local/fonts:/out" \
		deckops/sandbox:0.1.0 bash -c \
		'cp /usr/share/fonts/truetype/crosextra/*.ttf /usr/share/fonts/truetype/liberation/*.ttf /out/'
	@ls local/fonts | wc -l | sed 's/^/fonts available to the overflow predictor: /'

.PHONY: samples
samples:  ## build the engine's sample deck and example template, for the demo and the tests
	@mkdir -p local/samples
	@cd engine/deckops && ../../$(PY) samples/make_sample.py ../../local/samples >/dev/null
	@cd engine/deckops && ../../$(PY) samples/make_style_template.py ../../local/samples >/dev/null
	@ls local/samples

# ---------------------------------------------------------------- the stack

.PHONY: up
up:  ## start postgres and the S3 service
	$(COMPOSE) up -d postgres s3
	@$(COMPOSE) ps

.PHONY: down
down:  ## stop them (the data stays in the volumes)
	$(COMPOSE) down

.PHONY: destroy-data
destroy-data:  ## stop them and delete the volumes: every deck and every checkpoint goes too
	$(COMPOSE) down --volumes

.PHONY: logs
logs:  ## follow the services' logs
	$(COMPOSE) logs -f

# `local/` is scratch, but not all of it. Three things in there are depended on and are NOT
# removed: the metric-compatible fonts the overflow predictor measures with (`make fonts`),
# the sample decks the tests use (`make samples`), and anything under local/templates, which
# is where a prepared template and its kit live - a kit addresses shapes by id, so the file it
# was built from is not scrap. Everything else is run output and can go at any time; nothing
# of record is kept outside object storage.
# What to KEEP, not what to delete. Listing the things to remove cannot work: run output and
# one-off directories appear under local/ all the time, and the first version of this target
# already missed one. Anything not named here is output and goes.
KEEP_IN_LOCAL := fonts samples templates workspaces content.json handoff_content.json

.PHONY: clean-scratch
clean-scratch:  ## delete run output from local/, keeping fonts, samples and templates
	@before=$$(du -sk local 2>/dev/null | cut -f1); \
	for entry in local/*; do \
		name=$$(basename "$$entry"); \
		case " $(KEEP_IN_LOCAL) " in *" $$name "*) ;; *) rm -rf "$$entry";; esac; \
	done; \
	rm -rf local/workspaces; mkdir -p local/workspaces; \
	after=$$(du -sk local 2>/dev/null | cut -f1); \
	echo "local/ is now $$(du -sh local | cut -f1) (freed $$(( (before - after) / 1024 )) MB)"; \
	echo "kept: $$(ls local | tr '\n' ' ')"

# The bucket and Postgres. Each of these prints what it would delete; nothing goes without
# CONFIRM=1 (e.g. `make tidy CONFIRM=1`). `deck_agent.cli clean --help` has the finer options.
YES := $(if $(CONFIRM),--yes,)

.PHONY: ls
ls:  ## what is in the bucket, one line per deck
	@$(PY) -m deck_agent.cli ls

.PHONY: tidy
tidy:  ## drop older runs' previews/plans/ops from every deck, keeping each usable (CONFIRM=1)
	@$(PY) -m deck_agent.cli clean --tidy $(YES)

.PHONY: clean-tests
clean-tests:  ## decks and objects left behind by interrupted test runs (CONFIRM=1)
	@$(PY) -m deck_agent.cli clean --tests --orphans $(YES)

.PHONY: clean-all
clean-all:  ## every deck, session, kit and upload (CONFIRM=1)
	@$(PY) -m deck_agent.cli clean --all $(YES)

.PHONY: init
init:  ## create the checkpoint tables and the bucket
	$(PY) -m deck_agent.cli init

.PHONY: doctor
doctor:  ## check engine, fonts, sandbox, postgres, object storage, model
	@$(PY) -m deck_agent.cli doctor

.PHONY: selftest
selftest:  ## prove the sandbox has no network and a read-only root filesystem
	@$(PY) -m deck_agent.cli selftest

.PHONY: seed
seed:  ## put the sample template and a content pack in the bucket
	$(PY) -m deck_agent.cli seed

# ---------------------------------------------------------------- tests

.PHONY: test
test:  ## the tests that need nothing running (no containers, no postgres, no model)
	$(PY) -m pytest -q tests/unit

.PHONY: lint
lint:  ## unused imports and undefined names (pyflakes)
	@$(PY) -m pyflakes src runner engine/deckops tests | grep -v "__init__.py\|RecordedVision" || echo "clean"

.PHONY: test-all
test-all:  ## every test, including the ones that need docker, postgres and S3
	$(PY) -m pytest -q tests

.PHONY: test-engine
test-engine:  ## the engine's own tests (engine/deckops/tests)
	cd engine/deckops && ../../$(PY) -m pytest -q tests

.PHONY: demo
demo:  ## build a deck, edit it, translate it and use the escape hatch - with no model at all
	$(PY) tests/demo.py

.PHONY: serve
serve:  ## run the MCP + REST server on http://127.0.0.1:8765 (MCP at /mcp)
	$(PY) -m deck_agent.cli serve

.PHONY: graph
graph:  ## print the graph as mermaid
	@$(PY) -m deck_agent.cli graph
