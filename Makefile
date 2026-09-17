# Thin shim. The justfile is the source of truth: `make <target>` runs `just <target>`.
# Install just with: uv tool install rust-just
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory
.SUFFIXES:

JUST_VERSION := $(shell just --version)
ifeq ($(strip $(JUST_VERSION)),)
$(error just is not installed. Install it with: uv tool install rust-just)
endif

.PHONY: help
help: ## List available recipes (delegates to `just --list`)
	@just --list --unsorted

# Never try to rebuild this file through the catch-all rule.
Makefile: ;

# Forward every other target to the identically named just recipe.
%:
	@just $@
