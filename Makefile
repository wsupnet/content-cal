# ContentCal — developer convenience targets.
#
# Quick reference:
#   make help           List targets
#   make setup          One-time: build the test image + wire up git hooks
#   make test           Run the backend regression suite in Docker (~60s)
#   make test-local     Same, on your own Python 3.11+ (needs `make dev-deps`)
#   make install-hooks  Wire git to use the repo-versioned hooks in .githooks/
#
# Tests run inside the app's own Docker image (the `test` stage of
# backend/Dockerfile), so they behave the same on macOS, Windows and Linux
# with nothing installed beyond Docker.
#
# The pre-push hook runs the test suite before every `git push`. If you ever
# need to bypass it (rare, e.g. pushing a WIP branch), use `git push --no-verify`.

.PHONY: help setup test test-local test-image install-hooks dev-deps

help:
	@echo "ContentCal — make targets"
	@echo ""
	@echo "  make setup          Build the test image + wire up git hooks"
	@echo "  make test           Run the backend regression suite (in Docker)"
	@echo "  make test-local     Run it on your own Python 3.11+ instead"
	@echo "  make install-hooks  Configure git to use .githooks/ for hooks"
	@echo "  make dev-deps       Install Python dev dependencies locally (3.11+)"
	@echo ""

setup: test-image install-hooks
	@echo ""
	@echo "✓ Setup complete. Try: make test"

# Use `python3 -m pip` / `python3 -m pytest` so this works on macOS (where
# `python` isn't on PATH by default) and avoids depending on where pip
# installs the pytest entry-point script.
PY ?= python3

dev-deps:
	cd backend && $(PY) -m pip install -r requirements-dev.txt

test-image:
	docker build -q --target test -t contentcal-test backend

# backend/ is mounted at /app so edits are tested without a rebuild; the
# frontend is mounted where the tests expect it.
test: test-image
	docker run --rm -v "$(CURDIR)/backend:/app" -v "$(CURDIR)/frontend:/frontend:ro" contentcal-test

test-local:
	cd backend && $(PY) -m pytest -q

install-hooks:
	@if [ ! -d .git ]; then \
	  echo "✗ Not a git repository yet. Run 'git init' first."; \
	  exit 1; \
	fi
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-push
	@echo "✓ Git hooks installed. Pre-push will now run 'make test'."
	@echo "  Bypass with 'git push --no-verify' if you ever need to."
