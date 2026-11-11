# deckops-agent. `make help` lists everything.
SHELL := /bin/bash
PY    := .venv/bin/python
PIP   := .venv/bin/pip
COMPOSE := docker compose -f docker/compose.yml
ENGINE_REPO := https://github.com/pranaybahuguna/deckops

export PYTHONPATH := $(CURDIR)/src

.PHONY: help
help:  ## list the targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup

.PHONY: setup
setup: engine venv image fonts samples  ## everything needed to run, from a fresh clone
	@echo; echo "now: make up && make init && make doctor"

.PHONY: engine
engine:  ## clone the deckops engine and apply this project's patches
	@test -d engine/deckops/.git || git clone $(ENGINE_REPO) engine/deckops
	@cd engine/deckops && git rev-parse --short HEAD | sed 's/^/engine at /'
	@$(MAKE) --no-print-directory engine-patch

.PHONY: engine-patch
engine-patch:  ## apply patches/*.patch to the engine clone (idempotent)
	@for p in patches/*.patch; do \
		if git -C engine/deckops apply --check "$(CURDIR)/$$p" 2>/dev/null; then \
			git -C engine/deckops apply "$(CURDIR)/$$p" && echo "applied $$p"; \
		elif git -C engine/deckops apply --reverse --check "$(CURDIR)/$$p" 2>/dev/null; then \
			echo "already applied $$p"; \
		else \
			echo "COULD NOT APPLY $$p - see patches/README.md"; exit 1; \
		fi; \
	done

.PHONY: engine-reset
engine-reset:  ## drop the patches, returning the engine clone to upstream
	git -C engine/deckops checkout -- .

.PHONY: venv
venv:  ## create the virtualenv and install dependencies
	@test -d .venv || python3.12 -m venv .venv
	@$(PIP) install -q --upgrade pip
	@$(PIP) install -q -r requirements-dev.txt -r engine/deckops/requirements.txt
	@$(PY) --version

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

.PHONY: test-all
test-all:  ## every test, including the ones that need docker, postgres and S3
	$(PY) -m pytest -q tests

.PHONY: test-engine
test-engine:  ## the engine's own 28 tests, unchanged, as a regression check
	cd engine/deckops && ../../$(PY) -m pytest -q tests

.PHONY: demo
demo:  ## build a deck, edit it, translate it and use the escape hatch - with no model at all
	$(PY) tests/demo.py

.PHONY: graph
graph:  ## print the graph as mermaid
	@$(PY) -m deck_agent.cli graph
