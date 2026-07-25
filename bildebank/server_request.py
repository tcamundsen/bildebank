from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus
from typing import Any, Protocol


MAX_REQUEST_BODY_BYTES = 1024 * 1024


class RequestBodyError(ValueError):
    pass


class RequestBodyTooLarge(RequestBodyError):
    pass


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


def read_request_body_bytes(
    headers: Any,
    rfile: RequestBodyReader,
    *,
    max_bytes: int = MAX_REQUEST_BODY_BYTES,
) -> bytes:
    if max_bytes < 0:
        raise ValueError("max_bytes kan ikke være negativ")

    if header_values(headers, "Transfer-Encoding"):
        raise RequestBodyError("Transfer-Encoding støttes ikke.")

    content_lengths = header_values(headers, "Content-Length")
    if len(content_lengths) > 1:
        raise RequestBodyError("Flere Content-Length-headere er ikke tillatt.")
    if not content_lengths:
        return b""

    raw_length = content_lengths[0].strip()
    if (
        not raw_length
        or not raw_length.isascii()
        or not raw_length.isdecimal()
    ):
        raise RequestBodyError("Ugyldig Content-Length.")
    normalized_length = raw_length.lstrip("0") or "0"
    maximum_length = str(max_bytes)
    if (
        len(normalized_length) > len(maximum_length)
        or (
            len(normalized_length) == len(maximum_length)
            and normalized_length > maximum_length
        )
    ):
        raise RequestBodyTooLarge(
            f"Forespørselsinnholdet er større enn tillatt grense på "
            f"{max_bytes} byte."
        )
    length = int(normalized_length)
    body = rfile.read(length) if length else b""
    if len(body) != length:
        raise RequestBodyError(
            "Forespørselsinnholdet er kortere enn oppgitt Content-Length."
        )
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestBodyError(
            "Forespørselsinnholdet må være gyldig UTF-8."
        ) from exc
    return body


def header_values(headers: Any, name: str) -> list[str]:
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all(name)
        if values is not None:
            return [str(value) for value in values]
    value = headers.get(name)
    return [] if value is None else [str(value)]


def read_request_body(headers: Any, rfile: RequestBodyReader) -> str:
    return read_request_body_bytes(headers, rfile).decode("utf-8")


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
