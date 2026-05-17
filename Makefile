.PHONY: html

CMDS ?= face-scan face-suggest create import status make-thumbnails \
		list-sources show-source undelete

cli-help:
	@for cmd in $(CMDS); do \
		python tools/update_cli_help.py $$cmd; \
	done

html: cli-help
	python tools/gen-html-docs.py
