.PHONY: html

CMDS ?= face-scan face-suggest create import status make-thumbnails \
		list-sources show-source undelete \
		make-browser make-people-browser make-person-browser \
		non-metadata run-server remove

cli-help:
	@for cmd in $(CMDS); do \
		python tools/update_cli_help.py $$cmd; \
	done

html: cli-help
	python tools/gen-html-docs.py
