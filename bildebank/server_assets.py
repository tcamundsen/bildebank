from __future__ import annotations

import hashlib
import html
import importlib.resources
import urllib.parse


def _asset_text(filename: str) -> str:
    return importlib.resources.files(__package__).joinpath("assets", filename).read_text(encoding="utf-8")


def _asset_version(*contents: str) -> str:
    digest = hashlib.sha256()
    for content in contents:
        encoded = content.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


SERVER_CSS = _asset_text("server.css")
SERVER_JS = _asset_text("server.js")
SERVER_ASSET_VERSION = _asset_version(SERVER_CSS, SERVER_JS)


def page_html(title: str, body: str) -> str:
    asset_version = urllib.parse.quote(SERVER_ASSET_VERSION, safe="")
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/static/server.css?v={asset_version}">
</head>
<body>
{body}
<script src="/static/server.js?v={asset_version}"></script>
</body>
</html>
"""
