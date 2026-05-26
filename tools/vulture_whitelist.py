# Known false positives for `python -m vulture bildebank tests tools`.
#
# Keep this file small. New entries should explain an indirect use pattern,
# not hide code that should be removed.


class _VultureWhitelist:
    pass


_ = _VultureWhitelist()

# sqlite3.Connection.row_factory is assigned so query rows support name lookup.
_.row_factory

# Dataclass fields that are part of public result/status objects.
_.known_faces
_.stopped
_.search_results
_.compared
_.created_at
_.updated_at

# BaseHTTPRequestHandler hooks called by the stdlib HTTP server.
_.do_GET
_.log_message

# Cache-key-only parameters used to invalidate lru_cache entries when files change.
_.db_mtime_ns
_.face_db_mtime_ns

# Test helper intentionally kept for test setup readability.
_.enable_openclip_config
