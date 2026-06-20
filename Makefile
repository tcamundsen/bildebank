.PHONY: html cli-help dead-code clean-html clean-doc-stamps

DOCS_DIR := docs
HTML_DIR := html
TOOLS_DIR := tools
STAMP_DIR := .make/cli-help

# Utvid denne hvis flere Python-filer påvirker teksten i `bildebank <kommando> --help`.
CLI_DEPS ?= bildebank/cli.py

DOC_SOURCES := $(wildcard $(DOCS_DIR)/*.md $(DOCS_DIR)/web/*.md)
HTML_FILES := $(patsubst $(DOCS_DIR)/%.md,$(HTML_DIR)/%.html,$(DOC_SOURCES))
CLI_HELP_STAMPS := $(patsubst $(DOCS_DIR)/%.md,$(STAMP_DIR)/%.stamp,$(DOC_SOURCES))

html: $(HTML_FILES)

cli-help: $(CLI_HELP_STAMPS)

dead-code:
	python -m vulture bildebank tests tools --min-confidence 60

mtest:
	python -m ruff check bildebank tests tools
	python -m pyflakes bildebank tests tools
	mypy bildebank/*.py

radontest:
	python -m radon cc -s -a --min D bildebank/*.py

$(HTML_DIR)/%.html: $(DOCS_DIR)/%.md $(TOOLS_DIR)/gen-html-docs.py $(STAMP_DIR)/%.stamp
	@mkdir -p $(@D)
	python $(TOOLS_DIR)/gen-html-docs.py $< $@

$(STAMP_DIR)/%.stamp: $(DOCS_DIR)/%.md $(CLI_DEPS) $(TOOLS_DIR)/update_cli_help.py
	@mkdir -p $(@D)
	python $(TOOLS_DIR)/update_cli_help.py $<
	@touch $@

clean-html:
	rm -rf $(HTML_DIR)

clean-doc-stamps:
	rm -rf $(STAMP_DIR)
