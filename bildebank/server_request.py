from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus
from typing import Any, Protocol


class RequestBodyReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def first_param(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name, [])
    return values[0] if values else ""


def positive_int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    raw = first_param(params, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def nonnegative_int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    raw = first_param(params, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def parse_file_id(value: str) -> int:
    try:
        file_id = int(value)
    except ValueError as exc:
        raise ValueError("Ugyldig file_id.") from exc
    if file_id < 1:
        raise ValueError("Ugyldig file_id.")
    return file_id


def read_request_body(headers: Any, rfile: RequestBodyReader) -> str:
    length = int(headers.get("Content-Length") or "0")
    return rfile.read(length).decode("utf-8") if length > 0 else ""


def read_form_params(headers: Any, rfile: RequestBodyReader) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(read_request_body(headers, rfile))


def read_json_payload(headers: Any, rfile: RequestBodyReader) -> dict[str, object]:
    raw = read_request_body(headers, rfile)
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Ugyldig JSON.")
    return payload


def read_face_person_payload(
    headers: Any,
    rfile: RequestBodyReader,
) -> tuple[str, int] | tuple[dict[str, object], HTTPStatus]:
    raw = read_request_body(headers, rfile)
    content_type = headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = json.loads(raw or "{}")
        person_name = str(payload.get("person_name") or "").strip()
        face_id_raw = payload.get("face_id")
    else:
        params = urllib.parse.parse_qs(raw)
        person_name = first_param(params, "person_name").strip()
        face_id_raw = first_param(params, "face_id")
    try:
        face_id = int(face_id_raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Ugyldig face_id."}, HTTPStatus.BAD_REQUEST
    if not person_name:
        return {"ok": False, "error": "Personnavn mangler."}, HTTPStatus.BAD_REQUEST
    return person_name, face_id
