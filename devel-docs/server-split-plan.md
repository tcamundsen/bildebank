# Plan for splitting `bildebank/server.py`

## Goal

Make `run-server` easier to extend without changing behavior.

This is preparation for planned server features such as image tagging and
manual placement of images in H3 hexagons when GPS metadata is missing.

## Non-goals

- No behavior changes in the split commits.
- No UI redesign in the split commits.
- No database schema changes in the split commits.
- No broad rewrite of browser navigation, deletion, rotation, face assignment,
  or import behavior while splitting files.
- Do not rename user data files such as `.bilder.sqlite3`,
  `.bilder-faces.sqlite3`, or `.bilder-openclip.sqlite3`.

## Working Style

Do this on a separate branch, for example:

```bash
git switch -c refactor/split-server
```

Use small commits. Each commit should move one coherent cluster and should be
easy to revert independently.

After each commit, run:

```bash
python -m ruff check bildebank tests tools
python -m pyflakes bildebank tests tools
python -m pytest
make dead-code
```

If a move starts to require behavior changes, stop and either split the move
smaller or document the blocker here before continuing.

## Import Strategy

Use `bildebank/server.py` as the composition root at first.

The first split modules should be leaf modules:

- They should not import `bildebank.server`.
- They should return HTML or plain data.
- `BildebankServer.respond_*` methods can stay in `server.py` and call the new
  module functions.
- Pass state explicitly, for example `target`, `config`, `face_enabled`, and
  `openclip_enabled`.

To minimize churn, it is OK to move a function to a new module and import it
back into `server.py` with the same name:

```python
from .server_geo import geo_area_page_html
```

That keeps most call sites unchanged in the first commit.

## Techniques To Avoid Circular Imports

- Move leaf clusters first.
- Do not let split modules import `server.py`.
- Move small dataclasses/constants together with the functions that are their
  only users.
- Leave broad shared helpers in `server.py` until it is clear which module
  should own them.
- If a moved function needs many helpers from `server.py`, move fewer functions
  or postpone that cluster.
- Prefer explicit arguments over reading global server state.
- Avoid changing function behavior while moving it.
- Use `rg` before each move to find all references.
- Run tests after every cluster move.

## Proposed Modules

### `bildebank/server_markdown.py`

Scope:

- Markdown/help rendering.
- Markdown title extraction.
- Markdown inline/link handling.
- CLI-help marker stripping.

Reason:

- Low risk.
- Mostly pure string-to-HTML rendering.
- Good first split to validate the workflow.

Likely functions:

- `markdown_doc_page_html`
- `markdown_doc_title`
- `markdown_to_html`
- `strip_markdown_cli_help_markers`
- `markdown_inline_html`
- `markdown_link_html`
- `safe_markdown_link`

Status: started.

Notes:

- Pure markdown rendering helpers have been moved.
- `markdown_doc_page_html` remains in `server.py` for now because it uses
  `shell_page_html`, which is still a broad server composition helper.
- Reassess moving `markdown_doc_page_html` after generic page-shell helpers
  have a clearer home.

### `bildebank/server_search.py`

Scope:

- OpenCLIP server search cache and scoring.
- Search start/results pages.
- Search result rendering.

Reason:

- Fairly self-contained.
- Useful boundary before adding other browser-facing features.

Likely functions/classes:

- `SearchEmbeddingCache`
- `ServerSearchStats`
- `load_search_embedding_cache`
- `search_embedding_cache_key`
- `search_scores`
- `top_score_indexes`
- `search_server_images`
- `search_start_html`
- `search_html`
- `search_form`
- `result_html`

Status: started.

Notes:

- Search cache, embedding-cache loading, scoring, result rendering, and search
  form rendering have been moved.
- `search_start_html` and `search_html` remain in `server.py` for now because
  they use `shell_page_html` and server feature flags.
- Tests that patch OpenCLIP model/search internals should patch
  `bildebank.server_search`.

### `bildebank/server_geo.py`

Scope:

- Geo index/stats/area/missing pages.
- Custom geo places pages/forms.
- H3 map layout and SVG rendering.
- Geo place helpers that are only used by geo pages.

Reason:

- Important preparation for manual H3 placement.
- Keeps new geo mutation/UI work out of the already large `server.py`.

Likely functions/classes:

- `GeoMapCell`
- `geo_index_page_html`
- `custom_geo_places_page_html`
- `geo_place_rows`
- `geo_places_section_html`
- `geo_place_row_html`
- `h3geo_place_url`
- `custom_geo_places_admin_html`
- `custom_geo_place_form_html`
- `custom_geo_place_edit_html`
- `geo_map_page_html`
- `geo_filter_form_html`
- `h3_resolution_select_html`
- `geo_map_layout`
- `geo_component_pixel_coordinates`
- `geo_component_grid_coordinates`
- `geo_oriented_component_pixels`
- `geo_rotate_points`
- `geo_orientation_score`
- `geo_component_fallback_coordinates`
- `geo_map_svg_html`
- `geo_map_cell_svg`
- `geo_stats_page_html`
- `geo_area_page_html`
- `geo_parent_area_link_html`
- `geo_child_areas_section_html`
- `geo_missing_page_html`
- `geo_stats_summary_html`
- `geo_area_row_html`

Status: started.

Notes:

- Geo map dataclass, H3 layout helpers, orientation helpers, fallback
  coordinate helpers, and SVG rendering have been moved.
- Geo page leaf helpers such as filter forms, stats summary, area rows, and
  parent/child-area sections have been moved.
- Custom geo place HTML helpers have been moved.
- Geo place data helpers such as `geo_place_by_slug`,
  `geo_place_cells_by_column`, `geo_place_items`, `geo_area_items`,
  `geo_child_area_items`, `geo_missing_items`, and `geo_place_rows` have been
  moved.
- Geo page rendering remains in `server.py` while `shell_page_html` and shared
  browser helpers still live there.

### `bildebank/server_browser.py`

Scope:

- Browser source model and URL construction.
- Item lookup.
- Month navigation.
- Source/date/person filtering.
- Shared browser data access.

Reason:

- Central abstraction for future tag filters, source filters, geo filters, and
  combinations of filters.
- Higher risk than markdown/search/geo because many pages depend on it.

Likely functions/classes:

- `BrowserSource`
- `all_browser_source`
- `person_browser_source`
- `date_source_browser_source`
- `imported_source_browser_source`
- `geo_place_browser_source`
- `valid_browser_date_source`
- `is_filtered_source`
- `source_has_sql_filter`
- `source_sql_filter`
- `source_item_url`
- `source_month_url`
- `first_browser_item`
- `first_source_item`
- `first_sql_filtered_source_item`
- `first_unfiltered_source_item`
- `browser_item_by_id`
- `source_item_by_id`
- `adjacent_browser_items`
- `adjacent_source_items`
- `adjacent_sql_filtered_source_items`
- `item_order_key`
- `adjacent_items_from_list`
- `browser_month_keys`
- `source_month_keys`
- `cached_browser_month_keys`
- `sql_filtered_source_month_keys`
- `browser_month_navigation`
- `source_month_navigation`
- `source_month_navigation_for_key`
- `browser_month_navigation_for_key`
- `browser_month_items`
- `source_month_items`
- `sql_filtered_source_month_items`

Status: started.

Notes:

- Browser source model, person/source/geo source constructors, browser URL
  constructors, and SQL-filter helpers have been moved.
- Browser item lookup, adjacent navigation, month navigation, and HTML
  rendering remain in `server.py` for now.

### `bildebank/server_faces.py`

Scope:

- Person pages.
- Face overlay rendering.
- Registered people helpers.
- Face-box metadata cache helpers.
- Person assignment buttons/dialogs.

Reason:

- Keeps face/person UI work separate from generic browser routing.
- Should probably be moved after or alongside `server_browser.py`, because it
  depends on browser source/navigation concepts.

Likely functions/classes:

- `confirmed_people_for_file`
- `cached_confirmed_people_for_file`
- `person_item_url_for_face`
- `clear_face_caches`
- `registered_people`
- `cached_registered_people`
- `registered_people_rows`
- `active_file_id_set`
- `unconfirmed_faces_for_item`
- `unconfirmed_face_count_for_item`
- `person_by_name`
- `person_file_ids`
- `cached_person_file_ids`
- `person_items`
- `person_item_by_id`
- `adjacent_person_items`
- `person_month_navigation`
- `person_month_items`
- `person_faces_for_item`
- `cached_face_box_items_for_item`
- `cached_face_box_media_metadata`
- `face_box_media_metadata_from_item`
- `item_field`
- `file_mtime_ns`
- `update_face_box_media_metadata`
- `person_url`
- `person_item_url`
- `person_item_page_html`
- `person_item_media_html`
- `person_face_box_html`
- `unconfirm_face_buttons_html`
- `people_links_html`
- `people_link_html`
- `faces_button_html`
- `faces_overlay_html`
- `face_overlay_content_html`
- `face_overlay_item_html`
- `person_assignment_buttons_html`
- `people_page_html`
- `people_row_html`
- `person_rename_dialog_html`

Status: pending.

### `bildebank/server_app.py`

Scope:

- App/status/settings pages.
- Removed files page.
- Installed module/model status display.

Reason:

- Useful cleanup, but less urgent than geo/browser for planned features.

Likely functions:

- `app_status_page_html`
- `removed_files_page_html`
- `removed_file_row_html`
- `app_status_row_html`
- `app_status_face_config_row_html`
- `app_status_face_model_row_html`
- `selected_attr`
- `installed_insightface_models`
- `yes_no`
- `module_available`
- `server_program_repo_root`

Status: pending.

### `bildebank/server_actions.py`

Scope:

- Mutating browser actions such as remove/undelete/rotate.

Reason:

- This area touches image safety and should be moved late, only after the
  simpler splits have proven the approach.

Likely functions:

- `remove_file_from_browser`
- `undelete_file_from_browser`
- browser mutation helpers used by `respond_delete_item`,
  `respond_undelete_item`, and `respond_rotate_item`

Status: postponed.

## Suggested Order

1. Create this plan. Done.
2. Move markdown/help rendering to `server_markdown.py`. Started: pure
   markdown helpers moved; page wrapper remains in `server.py`.
3. Move OpenCLIP search helpers/pages to `server_search.py`. Started: cache,
   scoring, result rendering, and form rendering moved; page wrappers remain in
   `server.py`.
4. Move geo page/render helpers to `server_geo.py`. Started: layout/SVG,
   page leaf helpers, custom place forms, and geo data helpers moved; page
   wrappers remain in `server.py`.
5. Reassess dependencies before moving browser/navigation helpers. Started:
   browser source and URL helpers moved.
6. Move browser/navigation helpers if the dependency graph is clear. Started:
   source model and filter helpers moved; item/month data access remains.
7. Move face/person helpers after browser boundaries are stable.
8. Move app/status helpers if still useful.
9. Consider action/mutation helpers last.

## Status Log

- done: created initial split plan.
- done: moved pure markdown rendering helpers to `bildebank/server_markdown.py`.
- postponed: moving `markdown_doc_page_html`, because it currently depends on
  `shell_page_html` in `server.py`.
- done: moved OpenCLIP search cache/scoring helpers and result/form rendering
  to `bildebank/server_search.py`.
- postponed: moving `search_start_html` and `search_html`, because they
  currently depend on `shell_page_html` and server feature flags in `server.py`.
- done: moved geo map layout/orientation/SVG helpers to
  `bildebank/server_geo.py`.
- done: moved geo page leaf helpers to `bildebank/server_geo.py`.
- postponed: moving geo page wrappers such as `geo_map_page_html` and
  `geo_stats_page_html`, because they currently depend on `shell_page_html`.
- done: moved custom geo place HTML helpers to `bildebank/server_geo.py`.
- done: moved custom geo place data helpers such as `geo_place_rows`,
  `geo_place_by_slug`, and `geo_place_cells_by_column` to
  `bildebank/server_geo.py`.
- done: moved browser source model, source constructors, browser URL helpers,
  and SQL-filter helpers to `bildebank/server_browser.py`.
