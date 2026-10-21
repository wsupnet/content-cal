# ContentCal — developer convenience targets.
#
# Quick reference:
#   make help           List targets
#   make setup          One-time: install dev deps + wire up git hooks
#   make test           Run the backend regression suite (~30s)
#   make install-hooks  Wire git to use the repo-versioned hooks in .githooks/
#
# The pre-push hook runs the test suite before every `git push`. If you ever
# need to bypass it (rare, e.g. pushing a WIP branch), use `git push --no-verify`.

.PHONY: help setup test install-hooks dev-deps

help:
	@echo "ContentCal — make targets"
	@echo ""
	@echo "  make setup          Install dev dependencies + wire up git hooks"
	@echo "  make test           Run the backend regression suite"
	@echo "  make install-hooks  Configure git to use .githooks/ for hooks"
	@echo "  make dev-deps       Install Python dev dependencies only"
	@echo ""

setup: dev-deps install-hooks
	@echo ""
	@echo "✓ Setup complete. Try: make test"

# Use `python3 -m pip` / `python3 -m pytest` so this works on macOS (where
# `python` isn't on PATH by default) and avoids depending on where pip
# installs the pytest entry-point script.
PY ?= python3

dev-deps:
	cd backend && $(PY) -m pip install -r requirements-dev.txt

test:
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
