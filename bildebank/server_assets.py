from __future__ import annotations

import html
import urllib.parse


SERVER_ASSET_VERSION = "11"
SERVER_CSS = r"""    :root {
      color-scheme: dark;
      --bg: #171717;
      --panel: #242424;
      --stage: #0e0e0e;
      --border: #3a3a3a;
      --text: #f2f2f2;
      --muted: #b8b8b8;
      --accent: #7db7ff;
      --danger: #ff8a80;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .shell { max-width: 1200px; margin: 0 auto; padding: 24px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    .meta { color: var(--muted); margin: 0 0 18px; }
    .search-note { color: var(--muted); margin: 12px 0 0; font-size: 14px; }
    .search-loading {
      margin: 12px 0 0;
      padding: 10px 12px;
      border: 1px solid #4b6b8d;
      border-radius: 6px;
      background: #1d2a38;
      color: #d8ecff;
    }
    .search { display: grid; grid-template-columns: minmax(0, 1fr) 90px auto; gap: 8px; margin: 18px 0; }
    input, select, button {
      font: inherit;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #303030;
      color: var(--text);
    }
    button { cursor: pointer; }
    button:hover { background: #3a3a3a; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }
    .geo-filter { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 18px 0; }
    .geo-filter label { display: grid; gap: 4px; color: var(--muted); font-size: 13px; }
    .geo-filter input { width: 120px; }
    .geo-filter textarea, .custom-place-form textarea {
      width: min(520px, 78vw);
      min-height: 96px;
      resize: vertical;
      font: 13px ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #303030;
      color: var(--text);
    }
    .geo-name-form input[name="name"] { width: min(420px, 70vw); }
    .custom-geo-places { margin-top: 28px; }
    .custom-place-form {
      display: grid;
      grid-template-columns: minmax(240px, 360px) minmax(320px, 1fr) auto;
      gap: 12px;
      align-items: stretch;
      margin: 18px 0;
    }
    .custom-place-form label,
    .custom-place-identity {
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .custom-place-form input, .custom-place-form textarea { width: 100%; }
    .custom-place-actions {
      display: grid;
      gap: 8px;
      align-content: end;
    }
    .custom-place-actions button { min-height: 40px; white-space: nowrap; }
    .custom-place-list { display: grid; gap: 10px; margin-top: 12px; }
    .custom-place-edit {
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      overflow: hidden;
    }
    .custom-place-edit summary {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) minmax(120px, auto) auto;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      cursor: pointer;
      list-style: none;
    }
    .custom-place-edit summary::-webkit-details-marker { display: none; }
    .custom-place-edit summary:hover { background: #2b2b2b; }
    .custom-place-name { font-weight: 700; }
    .custom-place-edit-body {
      border-top: 1px solid var(--border);
      padding: 12px 14px 14px;
    }
    .custom-place-edit .custom-place-form { margin: 0; }
    @media (max-width: 900px) {
      .custom-place-form,
      .custom-place-edit summary {
        grid-template-columns: 1fr;
      }
    }
    .doc-page { max-width: 860px; }
    .doc-content { line-height: 1.6; }
    .doc-content h1, .doc-content h2, .doc-content h3 { margin: 1.2em 0 0.45em; }
    .doc-content p, .doc-content ul, .doc-content pre, .doc-content table { margin: 0 0 1em; }
    .doc-content code {
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      background: #303030;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 1px 4px;
    }
    .doc-content pre {
      overflow: auto;
      background: #101010;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px;
    }
    .doc-content pre code { background: transparent; border: 0; padding: 0; }
    .doc-content table { border-collapse: collapse; width: 100%; }
    .doc-content th, .doc-content td { border: 1px solid var(--border); padding: 6px 8px; text-align: left; }
    .doc-content th { background: var(--panel); }
    .markdown-alert {
      margin: 0 0 1em;
      padding: 8px 12px;
      border-left: 4px solid var(--border);
      color: var(--text);
    }
    .markdown-alert p { margin: 0; }
    .markdown-alert-title {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 4px;
      font-weight: 700;
    }
    .markdown-alert-warning { border-left-color: #ffd166; }
    .markdown-alert-warning .markdown-alert-title { color: #ffd166; }
    .geo-stats, .people-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin: 18px 0; }
    .geo-stats div, .people-summary div { display: grid; gap: 3px; padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
    .geo-stats span, .people-summary span { color: var(--muted); }
    .geo-list { display: grid; gap: 8px; margin-top: 18px; }
    .geo-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 12px; align-items: center; padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: var(--text); }
    .h3-cell-list .geo-row { grid-template-columns: minmax(220px, 1fr) minmax(70px, auto) minmax(90px, auto) minmax(180px, auto); }
    .geo-map-wrap { width: 100%; overflow: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); }
    .geo-map { display: block; min-width: 760px; width: 100%; height: auto; }
    .geo-hex { fill: #2f6f73; stroke: #8fd8dd; stroke-width: 2; }
    .geo-hex-link:hover .geo-hex { fill: #3f858a; }
    .geo-hex-count { fill: var(--text); font-size: 13px; font-weight: 700; pointer-events: none; }
    .server-browser { height: 100vh; overflow: hidden; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; }
    .server-browser.month-browser {
      min-height: 100vh;
      height: auto;
      overflow: visible;
      grid-template-rows: max-content minmax(0, 1fr) max-content;
    }
    .browser-header {
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 8px 10px;
      display: grid;
      gap: 7px;
    }
    .topline, .controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .title { font-weight: 700; margin-right: 8px; line-height: 1.2; }
    .breadcrumb { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .breadcrumb a { color: var(--text); text-decoration: none; }
    .breadcrumb a:hover { text-decoration: underline; }
    .breadcrumb .sep { color: var(--muted); font-weight: 400; }
    .status { color: var(--muted); font-size: 13px; line-height: 1.2; }
    .warning { color: #ffd166; font-size: 13px; line-height: 1.2; font-weight: 700; }
    .people { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .manual-person-form {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      max-width: 100%;
      font-size: 13px;
    }
    .manual-person-form label { color: var(--muted); }
    .manual-person-form select {
      min-width: 150px;
      max-width: 220px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 6px;
      background: #202020;
      color: var(--text);
    }
    .manual-person-form .assign-status { color: var(--muted); }
    .controls .delete-button { margin-left: auto; }
    .top-actions {
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .browser-header .topline {
       padding-bottom: 0px;
       padding-top: 0px;
    }
    .top-actions .server-search-link {
      border: 0;
      border-radius: 0;
      padding: 0;
      background: transparent;
      min-height: 0;
      color: var(--text);
    }
    .top-actions .server-search-link:hover {
      background: transparent;
      text-decoration: underline;
    }
    .subnav {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 18px;
    }
    .people-table { display: grid; gap: 8px; margin-top: 18px; }
    .removed-list { display: grid; gap: 6px; margin-top: 18px; }
    .removed-row {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto auto auto auto;
      gap: 10px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      font-size: 14px;
    }
    .removed-row span { color: var(--muted); }
    .people-row {
      display: grid;
      grid-template-columns: minmax(160px, 1fr) auto auto auto auto;
      gap: 8px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
    }
    .people-name { font-weight: 700; overflow-wrap: anywhere; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
    .rename-person-link {
      border: 0;
      padding: 0;
      background: transparent;
      color: var(--muted);
      font: inherit;
      font-size: 13px;
      font-weight: 400;
      cursor: pointer;
    }
    .rename-person-link:hover { color: var(--text); text-decoration: underline; }
    .people-warning { justify-self: start; }
    a, .disabled { color: var(--accent); }
    a { text-decoration: none; }
    a:hover { text-decoration: underline; }
    .nav-button, .server-search-link, .person-link, .faces-button {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 7px;
      background: #303030;
      color: var(--text);
      min-height: 32px;
      display: inline-flex;
      align-items: center;
    }
    .person-link { color: var(--accent); }
    .people {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: stretch;
    }
    .tag-rail .person-link {
      flex: 1 1 max-content;
      justify-content: center;
      text-align: center;
      white-space: nowrap;
    }
    .tag-rail .faces-button {
      flex: 0 0 auto;
      justify-content: center;
      text-align: center;
      white-space: nowrap;
    }
    .confirmed-badge {
      margin-left: 6px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1;
      color: var(--ok);
    }
    .faces-button { color: var(--accent); }
    .nav-button:hover, .server-search-link:hover, .person-link:hover, .faces-button:hover { background: #3a3a3a; text-decoration: none; }
    .danger-button { color: var(--danger); }
    .danger-button:hover { background: rgb(255 138 128 / 12%); }
    .disabled { color: #777; cursor: default; }
    .stage-shell {
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(132px, auto) minmax(0, 1fr);
      align-items: stretch;
      background: var(--stage);
      border-top: 1px solid var(--border);
      overflow: hidden;
    }
    .tag-rail {
      display: flex;
      flex-direction: column;
      gap: 8px;
      align-items: stretch;
      width: clamp(148px, 18vw, 260px);
      box-sizing: border-box;
      padding: 14px 10px;
      border-right: 1px solid var(--border);
      background: #141414;
    }
    .tag-toggle {
      min-height: 34px;
      padding: 6px 9px;
      border-color: rgb(255 255 255 / 20%);
      background: #242424;
      color: var(--muted);
      display: inline-flex;
      align-items: center;
      gap: 8px;
      text-align: left;
      white-space: nowrap;
    }
    .tag-toggle::before {
      content: "";
      width: 14px;
      height: 14px;
      box-sizing: border-box;
      border: 1px solid rgb(255 255 255 / 45%);
      border-radius: 3px;
      display: inline-grid;
      place-items: center;
      flex: 0 0 auto;
      color: #d8ecff;
      font-size: 12px;
      line-height: 1;
    }
    .tag-toggle:hover { color: var(--text); }
    .tag-toggle.active {
      border-color: #7db7ff;
      background: #1d344d;
      color: #d8ecff;
    }
    .tag-toggle.active::before {
      content: "✓";
      border-color: #9bccff;
      background: rgb(125 183 255 / 18%);
    }
    .tag-toggle:disabled { opacity: 0.65; cursor: default; }
    .date-status-badge {
      display: grid;
      gap: 3px;
      padding: 4px 4px 8px;
      color: var(--text);
      font-size: 12px;
      line-height: 1.25;
      text-align: center;
      overflow-wrap: anywhere;
    }
    .date-status-main { font-weight: 700; }
    .date-status-original { color: var(--muted); }
    .location-status-badge {
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      align-items: center;
      gap: 6px;
      padding: 2px 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
      text-align: center;
      overflow-wrap: anywhere;
    }
    .location-status-badge > a,
    .location-status-badge > .nav-button {
      flex: 0 1 auto;
    }
    .location-status-badge > .nav-button {
      min-height: 30px;
      padding: 4px 8px;
      white-space: normal;
    }
    .stage {
      position: relative;
      min-height: 0;
      height: 100%;
      display: grid;
      place-items: center;
      background: var(--stage);
      overflow: hidden;
    }
    .stage .media-link {
      display: grid;
      place-items: center;
      min-width: 0;
      min-height: 0;
      width: 100%;
      height: 100%;
      max-width: 100%;
      max-height: 100%;
    }
    .stage .media-link.quarter-turn {
      position: relative;
      width: 100%;
      height: 100%;
      overflow: visible;
    }
    .stage .media-link.quarter-turn > img {
      position: absolute;
      left: 50%;
      top: 50%;
      max-width: none;
      max-height: none;
    }
    .stage img, .stage video {
      min-width: 0;
      min-height: 0;
      width: auto;
      height: auto;
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
      display: block;
      transform-origin: center center;
    }
    .stage img[data-view-rotation="90"],
    .stage img[data-view-rotation="270"] {
      max-width: min(calc(100vh - 10rem), var(--quarter-turn-width, 100%));
      max-height: none;
    }
    .person-media[data-view-rotation="90"],
    .person-media[data-view-rotation="270"],
    .lightbox-media[data-view-rotation="90"],
    .lightbox-media[data-view-rotation="270"] {
      max-width: min(calc(100vh - 10rem), var(--quarter-turn-width, 100%));
      max-height: none;
    }
    .person-media[data-view-rotation="90"] img,
    .person-media[data-view-rotation="270"] img,
    .lightbox-media[data-view-rotation="90"] img,
    .lightbox-media[data-view-rotation="270"] img {
      max-height: none;
    }
    .person-media {
      position: relative;
      display: inline-block;
      max-width: min(100%, 92vw);
      max-height: calc(100vh - 10rem);
      transform-origin: center center;
    }
    .person-media img {
      max-width: 100%;
      max-height: calc(100vh - 10rem);
      object-fit: contain;
      display: block;
    }
    .person-face-box {
      position: absolute;
      border: 2px solid #2fbf71;
      background: rgb(47 191 113 / 13%);
      pointer-events: none;
    }
    .person-face-label {
      position: absolute;
      left: -2px;
      top: -24px;
      padding: 3px 6px;
      border-radius: 4px;
      background: rgb(0 0 0 / 78%);
      color: #fff;
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
    }
    .person-face-box.suggested {
      border-color: #e19b2d;
      background: rgb(225 155 45 / 14%);
    }
    .month-grid-server {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 14px;
      align-content: start;
      padding: 12px;
      overflow: auto;
    }
    .month-browser .month-grid-server { overflow: visible; }
    .thumb-link {
      display: grid;
      place-items: center;
      width: 100%;
      aspect-ratio: 4 / 3;
      overflow: hidden;
      color: inherit;
      text-decoration: none;
      background: #181818;
    }
    .thumb-link img, .video-thumb {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: grid;
      place-items: center;
      background: #181818;
      text-align: center;
    }
    .file-card {
      min-width: min(100%, 520px);
      max-width: min(100%, 720px);
      min-height: 180px;
      display: grid;
      place-items: center;
      padding: 16px;
      color: var(--muted);
      background: #181818;
      border: 1px solid var(--border);
      border-radius: 6px;
      text-align: center;
      text-decoration: none;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .item { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
    .item img { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; background: #181818; transform-origin: center center; }
    .text { padding: 10px; font-size: 14px; }
    .path { overflow-wrap: anywhere; }
    .score { color: var(--muted); margin-top: 4px; }
    .error { color: var(--danger); }
    .message { color: var(--muted); }
    .browser-footer {
      background: var(--panel);
      border-top: 1px solid var(--border);
      padding: 8px 12px;
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      align-items: center;
      min-width: 0;
    }
    .filename {
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      color: var(--muted);
    }
    .face-overlay {
      position: fixed;
      inset: 0;
      z-index: 10;
      background: rgb(0 0 0 / 86%);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 8px;
      padding: 12px;
    }
    .face-overlay[hidden] { display: none; }
    .info-overlay {
      position: fixed;
      inset: 0;
      z-index: 10;
      background: rgb(0 0 0 / 86%);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 8px;
      padding: 12px;
    }
    .info-overlay[hidden] { display: none; }
    .info-panel {
      align-self: start;
      justify-self: center;
      width: min(760px, 100%);
      max-height: 100%;
      overflow: auto;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 18px;
      color: var(--text);
    }
    .info-panel h2 { margin: 0 0 14px; font-size: 20px; }
    .modal-overlay {
      position: fixed;
      inset: 0;
      z-index: 10;
      display: grid;
      place-items: center;
      padding: 16px;
      background: rgb(0 0 0 / 72%);
    }
    .modal-overlay[hidden] { display: none; }
    .modal-panel {
      width: min(420px, 100%);
      display: grid;
      gap: 10px;
      padding: 18px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
    }
    .modal-panel h2 { margin: 0; font-size: 20px; }
    .modal-panel label { color: var(--muted); font-size: 13px; }
    .modal-panel input[type="text"],
    .modal-panel input[type="date"] {
      width: 100%;
      box-sizing: border-box;
      min-height: 36px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 8px;
      background: #181818;
      color: var(--text);
      font: inherit;
    }
    .manual-date-panel { align-self: start; justify-self: center; margin-top: 36px; }
    .manual-date-modes {
      display: grid;
      gap: 6px;
      margin: 0;
      padding: 0;
      border: 0;
    }
    .manual-date-modes label {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text);
    }
    .manual-date-modes input { width: auto; }
    .modal-actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
    .info-list { display: grid; gap: 0; margin: 0; }
    .info-row {
      display: grid;
      grid-template-columns: minmax(120px, 180px) minmax(0, 1fr);
      gap: 12px;
      padding: 9px 0;
      border-top: 1px solid var(--border);
    }
    .info-row:first-child { border-top: 0; }
    .info-row dt { color: var(--muted); }
    .info-row dd { margin: 0; overflow-wrap: anywhere; }
    .app-toggle-form { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .app-toggle { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; }
    .app-toggle input { position: absolute; opacity: 0; pointer-events: none; }
    .app-toggle-track {
      width: 44px;
      height: 24px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: #303030;
      padding: 2px;
      transition: background 120ms ease, border-color 120ms ease;
    }
    .app-toggle-track span {
      display: block;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: var(--muted);
      transition: transform 120ms ease, background 120ms ease;
    }
    .app-toggle input:checked + .app-toggle-track {
      border-color: #6fbf8f;
      background: #1f5c38;
    }
    .app-toggle input:checked + .app-toggle-track span {
      transform: translateX(20px);
      background: #d8ffe5;
    }
    .lightbox-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: #fff;
      font-size: 14px;
      min-width: 0;
    }
    .lightbox-title {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .lightbox-close {
      border-color: rgb(255 255 255 / 35%);
      background: rgb(255 255 255 / 10%);
      color: #fff;
      min-width: 42px;
    }
    .lightbox-stage {
      min-width: 0;
      min-height: 0;
      display: grid;
      place-items: center;
      overflow: auto;
    }
    .face-list {
      width: min(1200px, 100%);
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
      align-items: start;
    }
    .face-detail {
      display: grid;
      gap: 8px;
      color: #fff;
    }
    .face-detail-title {
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .lightbox-media {
      position: relative;
      display: inline-block;
      width: fit-content;
      max-width: 100%;
      justify-self: start;
      transform-origin: center center;
    }
    .lightbox-media img {
      display: block;
      max-width: calc(100vw - 24px);
      width: auto;
      height: auto;
    }
    .face-box {
      position: absolute;
      border: 3px solid #ff1f1f;
      background: rgb(255 31 31 / 12%);
      pointer-events: none;
    }
    .assign-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .new-person-form {
      display: grid;
      grid-template-columns: auto minmax(160px, 280px) auto;
      gap: 8px;
      align-items: center;
      justify-content: start;
    }
    .new-person-form label {
      color: var(--muted);
      font-size: 13px;
    }
    .assign-person-button {
      border-color: rgb(255 255 255 / 22%);
      background: rgb(255 255 255 / 10%);
      color: #fff;
      min-height: 34px;
      padding: 6px 10px;
    }
    .assign-person-button:hover { background: rgb(255 255 255 / 18%); }
    .assign-person-button:disabled { opacity: 0.55; cursor: default; }
    .assign-status { color: var(--muted); font-size: 13px; min-height: 1.3em; }
    @media (max-width: 640px) {
      .shell { padding: 16px; }
      .search { grid-template-columns: 1fr; }
      .browser-header { align-items: stretch; }
      .stage-shell { grid-template-columns: 1fr; }
      .tag-rail { width: auto; flex-direction: row; flex-wrap: wrap; border-right: 0; border-bottom: 1px solid var(--border); }
      .tag-toggle { justify-content: center; text-align: center; flex: 1 1 auto; }
      .nav-button, .server-search-link, .person-link, .faces-button { flex: 1 1 auto; justify-content: center; text-align: center; }
      .top-actions { margin-left: 0; width: 100%; justify-content: stretch; }
      .people-row { grid-template-columns: 1fr; align-items: stretch; }
      .removed-row { grid-template-columns: 1fr; align-items: stretch; }
      .geo-row { grid-template-columns: 1fr; }
      .new-person-form { grid-template-columns: 1fr; align-items: stretch; }
      .info-row { grid-template-columns: 1fr; gap: 4px; }
    }
"""
SERVER_JS = r"""  const faceOverlay = document.getElementById("faceOverlay");
  const infoOverlay = document.getElementById("infoOverlay");
  const openFacesButton = document.querySelector("[data-open-faces]");
  const closeFacesButton = document.querySelector("[data-close-faces]");
  const openInfoButtons = document.querySelectorAll("[data-open-info]");
  const closeInfoButton = document.querySelector("[data-close-info]");
  const faceList = faceOverlay?.querySelector("[data-face-list]");
  const infoList = infoOverlay?.querySelector("[data-info-list]");
  const manualDateOverlay = document.getElementById("manualDateOverlay");
  const manualDateForm = document.querySelector("[data-manual-date-form]");
  const manualDateStatus = document.querySelector("[data-manual-date-status]");
  const closeManualDateButtons = document.querySelectorAll("[data-close-manual-date]");
  const clearManualDateButton = document.querySelector("[data-clear-manual-date]");
  const manualDateFields = document.querySelectorAll("[data-manual-date-field]");
  const personRenameDialog = document.getElementById("personRenameDialog");
  const personRenameForm = document.querySelector("[data-person-rename-form]");
  const personRenameStatus = document.querySelector("[data-person-rename-status]");
  const closePersonRenameButton = document.querySelector("[data-close-person-rename]");
  const personRenameNameInput = personRenameForm?.querySelector('input[name="new_name"]');
  const personRenameOldNameInput = personRenameForm?.querySelector('input[name="old_name"]');
  const searchForm = document.querySelector("[data-search-form]");
  const searchLoading = document.querySelector("[data-search-loading]");
  let facesLoaded = false;
  let infoLoaded = false;
  let infoFileId = "";
  let manualDateFileId = "";
  document.addEventListener("submit", event => {
    const message = event.submitter?.dataset.confirmSubmit;
    if (message && !confirm(message)) event.preventDefault();
  });
  function faceStatusMessage(message) {
    const item = document.createElement("p");
    item.className = "empty";
    item.textContent = message;
    return item;
  }
  async function loadFacesOverlay() {
    if (!faceList || facesLoaded) return;
    const fileId = openFacesButton?.dataset.facesItem || "";
    if (!fileId) return;
    faceList.replaceChildren(faceStatusMessage("Laster..."));
    try {
      const response = await fetch(`/api/item-faces?file_id=${encodeURIComponent(fileId)}`);
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke laste ansikter.");
      faceList.innerHTML = payload.html || "";
      bindFaceAssignmentHandlers(faceList);
      facesLoaded = true;
    } catch (error) {
      faceList.replaceChildren(faceStatusMessage(error.message || "Kunne ikke laste ansikter."));
    }
  }
  async function openFacesOverlay() {
    if (!faceOverlay) return;
    faceOverlay.hidden = false;
    await loadFacesOverlay();
    closeFacesButton?.focus();
  }
  function closeFacesOverlay() {
    if (!faceOverlay) return;
    faceOverlay.hidden = true;
  }
  function infoStatusRow(message) {
    const row = document.createElement("div");
    row.className = "info-row";
    const label = document.createElement("dt");
    label.textContent = "Status";
    const value = document.createElement("dd");
    value.textContent = message;
    row.append(label, value);
    return row;
  }
  async function loadInfoOverlay(fileId) {
    if (!infoList) return;
    if (infoLoaded && infoFileId === fileId) return;
    if (!fileId) return;
    infoLoaded = false;
    infoFileId = fileId;
    infoList.replaceChildren(infoStatusRow("Laster..."));
    try {
      const response = await fetch(`/api/item-info?file_id=${encodeURIComponent(fileId)}`);
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke laste bildeinfo.");
      infoList.innerHTML = payload.html || "";
      infoLoaded = true;
    } catch (error) {
      infoList.replaceChildren(infoStatusRow(error.message || "Kunne ikke laste bildeinfo."));
    }
  }
  async function openInfoOverlay(opener) {
    if (!infoOverlay) return;
    infoOverlay.hidden = false;
    const fileId = opener?.dataset.infoItem || openInfoButtons[0]?.dataset.infoItem || "";
    await loadInfoOverlay(fileId);
    closeInfoButton?.focus();
  }
  function closeInfoOverlay() {
    if (!infoOverlay) return;
    infoOverlay.hidden = true;
  }
  function fitQuarterTurnMediaLink(img) {
    const link = img.closest(".media-link.quarter-turn");
    const stage = img.closest(".stage");
    if (!link || !stage || !img.naturalWidth || !img.naturalHeight) return;
    const stageRect = stage.getBoundingClientRect();
    const availableWidth = Math.max(stageRect.width, 1);
    const availableHeight = Math.max(stageRect.height, 1);
    const scale = Math.max(Math.min(availableWidth / img.naturalHeight, availableHeight / img.naturalWidth), 0.01);
    const originalWidth = img.naturalWidth * scale;
    const originalHeight = img.naturalHeight * scale;
    const rotation = img.dataset.viewRotation || "90";
    link.style.width = `${originalHeight}px`;
    link.style.height = `${originalWidth}px`;
    img.style.width = `${originalWidth}px`;
    img.style.height = `${originalHeight}px`;
    img.style.transform = `translate(-50%, -50%) rotate(${rotation}deg)`;
  }
  function fitQuarterTurnLegacyMedia(item) {
      const stage = item.closest(".stage");
      if (!stage) return;
      const img = item instanceof HTMLImageElement ? item : item.querySelector("img");
      if (!(img instanceof HTMLImageElement)) return;
      if (!img.naturalWidth || !img.naturalHeight) return;
      const stageRect = stage.getBoundingClientRect();
      const availableWidth = Math.max(stageRect.width, 1);
      const availableHeight = Math.max(stageRect.height, 1);
      const ratio = img.naturalWidth / img.naturalHeight;
      const maxOriginalWidth = Math.max(Math.min(availableHeight, availableWidth * ratio), 1);
      item.style.maxWidth = `${maxOriginalWidth}px`;
      item.style.maxHeight = "none";
  }
  function fitQuarterTurnMedia() {
    document.querySelectorAll('.stage .media-link.quarter-turn > img[data-view-rotation="90"], .stage .media-link.quarter-turn > img[data-view-rotation="270"]').forEach(img => {
      if (img instanceof HTMLImageElement) fitQuarterTurnMediaLink(img);
    });
    document.querySelectorAll('.stage .person-media[data-view-rotation="90"], .stage .person-media[data-view-rotation="270"]').forEach(fitQuarterTurnLegacyMedia);
  }
  function scheduleQuarterTurnFit() {
    requestAnimationFrame(fitQuarterTurnMedia);
  }
  function manualDateInput(name) {
    return manualDateForm?.querySelector(`[name="${name}"]`);
  }
  function selectedManualDateMode() {
    return manualDateInput("mode")?.checked ? manualDateInput("mode").value : manualDateForm?.querySelector('[name="mode"]:checked')?.value || "exact";
  }
  function setManualDateMode(mode) {
    manualDateForm?.querySelectorAll('[name="mode"]').forEach(input => {
      input.checked = input.value === mode;
    });
    updateManualDateFields();
  }
  function updateManualDateFields() {
    const mode = selectedManualDateMode();
    manualDateFields.forEach(label => {
      const field = label.dataset.manualDateField || "";
      const visible = ((mode === "exact" || mode === "uncertain") && field === "date") || (mode === "uncertain" && field === "uncertainty") || (mode === "between" && (field === "date_from" || field === "date_to"));
      label.hidden = !visible;
      label.querySelectorAll("input").forEach(input => {
        input.disabled = !visible;
      });
    });
  }
  function midpointIsoDate(dateFrom, dateTo) {
    if (!dateFrom || !dateTo) return "";
    const start = new Date(`${dateFrom}T00:00:00Z`);
    const end = new Date(`${dateTo}T00:00:00Z`);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return dateFrom;
    return new Date(start.getTime() + Math.floor((end.getTime() - start.getTime()) / 2)).toISOString().slice(0, 10);
  }
  function openManualDateOverlay(button) {
    if (!manualDateOverlay || !manualDateForm) return;
    manualDateFileId = button.dataset.manualDateItem || "";
    const manualFrom = button.dataset.manualDateFrom || "";
    const manualTo = button.dataset.manualDateTo || "";
    const manualNote = button.dataset.manualDateNote || "";
    manualDateForm.reset();
    manualDateInput("date").value = midpointIsoDate(manualFrom, manualTo);
    manualDateInput("date_from").value = manualFrom;
    manualDateInput("date_to").value = manualTo;
    manualDateInput("note").value = manualNote;
    setManualDateMode(manualFrom && manualTo && manualFrom !== manualTo ? "between" : "exact");
    if (manualDateStatus) manualDateStatus.textContent = "";
    if (clearManualDateButton) clearManualDateButton.hidden = !(manualFrom && manualTo);
    manualDateOverlay.hidden = false;
    manualDateInput("date")?.focus();
  }
  function closeManualDateOverlay() {
    if (!manualDateOverlay) return;
    manualDateOverlay.hidden = true;
  }
  function openPersonRenameDialog(name) {
    if (!personRenameDialog || !personRenameForm || !personRenameNameInput || !personRenameOldNameInput) return;
    personRenameOldNameInput.value = name || "";
    personRenameNameInput.value = name || "";
    if (personRenameStatus) personRenameStatus.textContent = "";
    personRenameDialog.hidden = false;
    personRenameNameInput.focus();
    personRenameNameInput.select();
  }
  function closePersonRenameDialog() {
    if (!personRenameDialog) return;
    personRenameDialog.hidden = true;
  }
  function ensureTopPersonLink(name, url, confirmed = false) {
    if (!name || !url) return;
    let people = document.querySelector(".topline .people");
    if (!people) {
      people = document.createElement("div");
      people.className = "people";
      document.querySelector(".topline .title")?.after(people);
    }
    const exists = Array.from(people.querySelectorAll(".person-link")).some(link => link.dataset.personName === name);
    if (exists) return;
    const link = document.createElement("a");
    link.className = "person-link";
    link.href = url;
    link.dataset.personName = name;
    link.append(document.createTextNode(name));
    if (confirmed) {
      const badge = document.createElement("span");
      badge.className = "confirmed-badge";
      badge.title = "Bekreftet";
      badge.setAttribute("aria-label", "Bekreftet");
      badge.textContent = " ✅";
      link.append(badge);
    }
    people.append(link);
  }
  document.querySelectorAll('.stage img[data-view-rotation="90"], .stage img[data-view-rotation="270"], .stage .person-media[data-view-rotation="90"] img, .stage .person-media[data-view-rotation="270"] img').forEach(img => {
    if (img instanceof HTMLImageElement && !img.complete) {
      img.addEventListener("load", scheduleQuarterTurnFit, {once: true});
    }
  });
  window.addEventListener("resize", scheduleQuarterTurnFit);
  scheduleQuarterTurnFit();
  openFacesButton?.addEventListener("click", openFacesOverlay);
  closeFacesButton?.addEventListener("click", closeFacesOverlay);
  openInfoButtons.forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      openInfoOverlay(button);
    });
  });
  closeInfoButton?.addEventListener("click", closeInfoOverlay);
  closeManualDateButtons.forEach(button => {
    button.addEventListener("click", closeManualDateOverlay);
  });
  document.querySelectorAll("[data-open-manual-date]").forEach(button => {
    button.addEventListener("click", () => openManualDateOverlay(button));
  });
  manualDateForm?.querySelectorAll('[name="mode"]').forEach(input => {
    input.addEventListener("change", updateManualDateFields);
  });
  manualDateForm?.addEventListener("submit", async event => {
    event.preventDefault();
    if (!manualDateFileId) return;
    if (manualDateStatus) manualDateStatus.textContent = "Lagrer...";
    manualDateForm.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await fetch("/api/item-manual-date", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          file_id: Number(manualDateFileId),
          mode: selectedManualDateMode(),
          date: manualDateInput("date")?.value || "",
          uncertainty: manualDateInput("uncertainty")?.value || "",
          date_from: manualDateInput("date_from")?.value || "",
          date_to: manualDateInput("date_to")?.value || "",
          note: manualDateInput("note")?.value || "",
        }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke lagre dato.");
      window.location.reload();
    } catch (error) {
      if (manualDateStatus) manualDateStatus.textContent = error.message || "Kunne ikke lagre dato.";
      manualDateForm.querySelectorAll("button, input").forEach(item => item.disabled = false);
      updateManualDateFields();
    }
  });
  clearManualDateButton?.addEventListener("click", async () => {
    if (!manualDateFileId) return;
    if (!confirm("Fjerne manuell dato fra bildet?")) return;
    if (manualDateStatus) manualDateStatus.textContent = "Fjerner...";
    manualDateForm?.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await fetch("/api/item-manual-date-clear", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({file_id: Number(manualDateFileId)}),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke fjerne dato.");
      window.location.reload();
    } catch (error) {
      if (manualDateStatus) manualDateStatus.textContent = error.message || "Kunne ikke fjerne dato.";
      manualDateForm?.querySelectorAll("button, input").forEach(item => item.disabled = false);
      updateManualDateFields();
    }
  });
  searchForm?.addEventListener("submit", () => {
    if (searchForm.dataset.modelLoaded === "true") return;
    if (searchLoading) searchLoading.hidden = false;
  });
  closePersonRenameButton?.addEventListener("click", closePersonRenameDialog);
  document.querySelectorAll("[data-open-person-rename]").forEach(button => {
    button.addEventListener("click", () => openPersonRenameDialog(button.dataset.personName || ""));
  });
  document.querySelectorAll("[data-delete-person-name]").forEach(button => {
    button.addEventListener("click", async () => {
      const personName = button.dataset.deletePersonName || "";
      if (!personName) return;
      const command = `bildebank face-person-delete "${personName}"`;
      if (!confirm(`Slette personen ${personName} fra ansiktsdatabasen?\n\nDette sletter bekreftede ansiktskoblinger og forslag for personen, men ingen bilder.\n\nTilsvarer:\n${command}`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/face-person-delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({person_name: personName}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke slette person.");
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke slette person.");
        button.disabled = false;
      }
    });
  });
  personRenameDialog?.addEventListener("click", event => {
    if (event.target === personRenameDialog) closePersonRenameDialog();
  });
  personRenameForm?.addEventListener("submit", async event => {
    event.preventDefault();
    const oldName = personRenameOldNameInput?.value || "";
    const newName = personRenameNameInput?.value?.trim() || "";
    if (personRenameStatus) personRenameStatus.textContent = "Lagrer...";
    personRenameForm.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await fetch("/api/face-person-rename", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({old_name: oldName, new_name: newName}),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke endre navn.");
      window.location.reload();
    } catch (error) {
      if (personRenameStatus) personRenameStatus.textContent = error.message || "Kunne ikke endre navn.";
      personRenameForm.querySelectorAll("button, input").forEach(item => item.disabled = false);
      personRenameNameInput?.focus();
    }
  });
  document.querySelectorAll("[data-rotate-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.rotateItem);
      const direction = button.dataset.rotateDirection || "";
      button.disabled = true;
      try {
        const response = await fetch("/api/item-rotate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, direction}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke rotere.");
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke rotere.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-tag-toggle]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.tagToggle);
      const tagName = button.dataset.tagName || "";
      const tagged = button.getAttribute("aria-pressed") !== "true";
      button.disabled = true;
      try {
        const response = await fetch("/api/item-tag", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, tag_name: tagName, tagged}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke lagre tagg.");
        const encodedTag = encodeURIComponent(payload.tag_name || tagName);
        const hideRedirect = button.dataset.tagHideRedirect || "";
        if (payload.tagged && hideRedirect && (payload.tag_name || tagName) === "Ute av fokus") {
          window.location.href = hideRedirect;
          return;
        }
        if (!payload.tagged && window.location.pathname.startsWith(`/tag/${encodedTag}/`)) {
          window.location.href = `/tag/${encodedTag}`;
          return;
        }
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke lagre tagg.");
        button.disabled = false;
      }
    });
  });
  async function setManualLocation(button) {
    if (!button || button.disabled) return;
    const fileId = Number(button.dataset.manualLocationItem);
    if (!fileId) return;
    button.disabled = true;
    try {
      const response = await fetch("/api/item-manual-location", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({file_id: fileId}),
      });
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "Kunne ikke sette sted.");
      window.location.reload();
    } catch (error) {
      alert(error.message || "Kunne ikke sette sted.");
      button.disabled = false;
    }
  }
  document.querySelectorAll("[data-manual-location-item]").forEach(button => {
    button.addEventListener("click", () => setManualLocation(button));
  });
  async function removeManualLocation(button) {
    if (!button || button.disabled) return;
    const fileId = Number(button.dataset.removeManualLocationItem);
    if (!fileId) return;
    if (!confirm("Fjerne manuell H3-lokasjon fra bildet?")) return;
    button.disabled = true;
    try {
      const response = await fetch("/api/item-manual-location-remove", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({file_id: fileId}),
      });
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "Kunne ikke fjerne manuelt sted.");
      window.location.reload();
    } catch (error) {
      alert(error.message || "Kunne ikke fjerne manuelt sted.");
      button.disabled = false;
    }
  }
  document.querySelectorAll("[data-remove-manual-location-item]").forEach(button => {
    button.addEventListener("click", () => removeManualLocation(button));
  });
  document.querySelectorAll("[data-delete-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.deleteItem);
      const path = button.dataset.deletePath || "";
      const redirectUrl = button.dataset.deleteRedirect || "/";
      if (!confirm(`Flytte til deleted/?\n\n${path}`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/item-delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke slette.");
        window.location.href = redirectUrl;
      } catch (error) {
        alert(error.message || "Kunne ikke slette.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-undelete-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.undeleteItem);
      const path = button.dataset.undeletePath || "";
      if (!confirm(`Flytte tilbake til bildesamlingen?\n\n${path}`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/item-undelete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke angre sletting.");
        button.closest(".removed-row")?.remove();
      } catch (error) {
        alert(error.message || "Kunne ikke angre sletting.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-unconfirm-face]").forEach(button => {
    button.addEventListener("click", async () => {
      const faceId = Number(button.dataset.unconfirmFace);
      const personName = button.dataset.unconfirmPerson || "";
      if (!faceId || !personName) return;
      const command = `bildebank face-person-remove-face "${personName}" ${faceId}`;
      if (!confirm(`Avbekrefte face-id ${faceId} fra ${personName}?\n\nTilsvarer:\n${command}`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/face-person-remove-face", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({face_id: faceId, person_name: personName}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke avbekrefte.");
        if (payload.redirect_url) {
          window.location.href = payload.redirect_url;
          return;
        }
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke avbekrefte.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-manual-person-form]").forEach(form => {
    const select = form.querySelector('select[name="person_name"]');
    const status = form.querySelector("[data-manual-person-status]");
    const fileId = Number(form.dataset.fileId);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const personName = select?.value || "";
      if (!fileId || !personName) return;
      if (status) status.textContent = "Lagrer...";
      form.querySelectorAll("button, select").forEach(item => item.disabled = true);
      try {
        const response = await fetch("/api/face-person-add-file", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, person_name: personName}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke lagre person.");
        ensureTopPersonLink(payload.person_name, payload.person_url, true);
        if (status) status.textContent = "Lagret.";
        form.querySelectorAll("button, select").forEach(item => item.disabled = false);
      } catch (error) {
        if (status) status.textContent = error.message || "Kunne ikke lagre person.";
        form.querySelectorAll("button, select").forEach(item => item.disabled = false);
      }
    });
    form.querySelector("[data-manual-person-remove]")?.addEventListener("click", async () => {
      const personName = select?.value || "";
      if (!fileId || !personName) return;
      if (status) status.textContent = "Fjerner...";
      form.querySelectorAll("button, select").forEach(item => item.disabled = true);
      try {
        const response = await fetch("/api/face-person-remove-file", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, person_name: personName}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke fjerne person.");
        window.location.reload();
      } catch (error) {
        if (status) status.textContent = error.message || "Kunne ikke fjerne person.";
        form.querySelectorAll("button, select").forEach(item => item.disabled = false);
      }
    });
  });
  document.querySelectorAll("[data-remove-person-file]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.removePersonFile);
      const personName = button.dataset.removePersonFilePerson || "";
      if (!fileId || !personName) return;
      if (!confirm(`Fjerne manuell person-i-bilde for ${personName}?`)) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/face-person-remove-file", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, person_name: personName}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke fjerne person.");
        window.location.href = payload.person_url || `/person/${encodeURIComponent(personName)}`;
      } catch (error) {
        alert(error.message || "Kunne ikke fjerne person.");
        button.disabled = false;
      }
    });
  });
  faceOverlay?.addEventListener("click", event => {
    if (event.target === faceOverlay || event.target.classList?.contains("lightbox-stage")) closeFacesOverlay();
  });
  infoOverlay?.addEventListener("click", event => {
    if (event.target === infoOverlay) closeInfoOverlay();
  });
  async function assignFace(detail, status, endpoint, faceId, personName) {
    if (!detail || !status || !faceId || !personName) return;
    status.textContent = "Lagrer...";
    detail.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({face_id: Number(faceId), person_name: personName}),
      });
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "Kunne ikke lagre.");
      status.textContent = `Koblet til ${payload.person_name}.`;
      ensureTopPersonLink(payload.person_name, payload.person_url, payload.confirmed);
      detail.remove();
      if (!document.querySelector(".face-detail")) {
        closeFacesOverlay();
        window.location.reload();
      }
    } catch (error) {
      status.textContent = error.message || "Kunne ikke lagre.";
      detail.querySelectorAll("button, input").forEach(item => item.disabled = false);
    }
  }
  function bindFaceAssignmentHandlers(root = document) {
    root.querySelectorAll(".assign-person-button").forEach(button => {
      button.addEventListener("click", async () => {
        const faceId = button.dataset.faceId;
        const personName = button.dataset.personName;
        const detail = button.closest(".face-detail");
        const status = detail?.querySelector(".assign-status");
        await assignFace(detail, status, "/api/face-person-add-face", faceId, personName);
      });
    });
    root.querySelectorAll("[data-new-person-form]").forEach(form => {
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const detail = form.closest(".face-detail");
        const status = detail?.querySelector(".assign-status");
        const faceId = form.querySelector('input[name="face_id"]')?.value;
        const personName = form.querySelector('input[name="person_name"]')?.value?.trim();
        await assignFace(detail, status, "/api/face-person-create-and-add-face", faceId, personName);
      });
    });
  }
  bindFaceAssignmentHandlers();
  document.addEventListener("keydown", event => {
    if (faceOverlay && !faceOverlay.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeFacesOverlay();
      }
      return;
    }
    if (infoOverlay && !infoOverlay.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeInfoOverlay();
      }
      return;
    }
    if (personRenameDialog && !personRenameDialog.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closePersonRenameDialog();
      }
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const target = event.target;
    if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target instanceof HTMLButtonElement ||
      target?.isContentEditable
    ) return;
    if (event.key.toLowerCase() === "g") {
      const button = document.querySelector("[data-manual-location-item]");
      if (!(button instanceof HTMLButtonElement)) return;
      event.preventDefault();
      setManualLocation(button);
      return;
    }
    const selector = {
      ArrowLeft: '[data-key-nav="previous"]',
      ArrowRight: '[data-key-nav="next"]',
      ArrowUp: '[data-key-nav="previous-month"]',
      ArrowDown: '[data-key-nav="next-month"]',
      PageUp: '[data-key-nav="previous-year"]',
      PageDown: '[data-key-nav="next-year"]',
    }[event.key] || "";
    if (!selector) return;
    const link = document.querySelector(selector);
    if (!(link instanceof HTMLAnchorElement)) return;
    event.preventDefault();
    window.location.href = link.href;
  });
"""


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
