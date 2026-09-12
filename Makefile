SHELL := /bin/bash

UV ?= uv
PYTHON ?= python3

NAME         := auto-review-pr-dashboard
WRAPPER_SRC  := $(CURDIR)/scripts/$(NAME).bash
BASHRC_D     := $(HOME)/.bashrc.d
WRAPPER_LINK := $(BASHRC_D)/$(NAME)
SOCKET_DIR   := /tmp/$(NAME)-$(shell id -u)

.DEFAULT_GOAL := deps
.PHONY: deps install uninstall test clean requirements

# Dependencies only: a local uv-managed .venv for development and tests.
deps:
	$(UV) sync

# Optional, and never run by another target: writes a pinned requirements.txt from
# uv.lock for environments that want one. pyproject.toml stays the source of truth for
# ranges and uv.lock for pins, so the generated file is gitignored, not committed.
# Needs uv; without uv, install the project itself instead (pip install .).
requirements:
	$(UV) export --quiet --format requirements-txt --no-hashes --no-dev \
		--no-emit-project -o requirements.txt
	@echo "wrote requirements.txt from uv.lock - generated, do not commit"

# The CLI itself, plus the shell wrapper that exports a bash-function ai_cli.
install:
	$(UV) tool install --force .
	mkdir -p $(BASHRC_D)
	ln -sfn $(WRAPPER_SRC) $(WRAPPER_LINK)
	@echo
	@echo "installed:"
	@echo "  CLI      $$(type -P $(NAME) || echo '<not on PATH - run: uv tool update-shell>')"
	@echo "  wrapper  $(WRAPPER_LINK) -> $(WRAPPER_SRC)"
	@echo
	@echo "open a new shell, or: source $(WRAPPER_LINK)"

# Reverses install. Refuses while a daemon is still running, since removing the
# socket directory would orphan it.
uninstall:
	@if [ -z "$(FORCE)" ] && pgrep -u "$$(id -u)" -f "auto_review_pr_dashboard\.session" >/dev/null 2>&1; then \
		echo "$(NAME): a daemon is still running - quit it with 'q' in the dashboard first"; \
		echo "  ($(NAME) -l lists the working directories; or force with: make uninstall FORCE=1)"; \
		exit 1; \
	fi
	-$(UV) tool uninstall $(NAME)
	@if [ "$$(readlink -f $(WRAPPER_LINK) 2>/dev/null)" = "$$(readlink -f $(WRAPPER_SRC))" ]; then \
		rm -f $(WRAPPER_LINK); echo "removed $(WRAPPER_LINK)"; \
	elif [ -e $(WRAPPER_LINK) ] || [ -L $(WRAPPER_LINK) ]; then \
		echo "kept $(WRAPPER_LINK): it does not point at this repo"; \
	fi
	rm -rf $(SOCKET_DIR)
	@echo
	@echo "note: open shells keep the function until they restart (unset -f $(NAME) clears it)"
	@echo "      review artifacts under .tmp/$(NAME)/ are left alone; 'make clean' removes .venv"

test:
	$(UV) run $(PYTHON) -m unittest discover -s tests

clean:
	rm -rf .venv
	rm -f requirements.txt
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
