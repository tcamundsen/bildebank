from __future__ import annotations

import ipaddress
import json
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Protocol


MAX_REQUEST_BODY_BYTES = 1024 * 1024


class RequestBodyError(ValueError):
    pass


class RequestBodyTooLarge(RequestBodyError):
    pass


class RequestAuthorityError(ValueError):
    def __init__(self, message: str, *, status: HTTPStatus) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HttpAuthority:
    host: str
    port: int | None
    is_ip_literal: bool


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


def validate_request_authority(
    headers: Any,
    *,
    bind_host: str,
    server_host: str,
    server_port: int,
) -> None:
    host_values = header_values(headers, "Host")
    if len(host_values) != 1:
        raise RequestAuthorityError(
            "Forespørselen må ha nøyaktig én Host-header.",
            status=HTTPStatus.BAD_REQUEST,
        )
    authority = parse_http_authority(host_values[0])
    if authority.port is not None and authority.port != server_port:
        raise RequestAuthorityError(
            "Host-headeren bruker feil port.",
            status=HTTPStatus.MISDIRECTED_REQUEST,
        )
    if not request_host_is_allowed(
        authority,
        bind_host=bind_host,
        server_host=server_host,
    ):
        raise RequestAuthorityError(
            "Host-headeren er ikke tillatt for denne Bildebank-serveren.",
            status=HTTPStatus.MISDIRECTED_REQUEST,
        )

    origin_values = header_values(headers, "Origin")
    if not origin_values:
        return
    if len(origin_values) != 1:
        raise RequestAuthorityError(
            "Forespørselen kan ikke ha flere Origin-headere.",
            status=HTTPStatus.BAD_REQUEST,
        )
    validate_request_origin(
        origin_values[0],
        authority=authority,
        server_port=server_port,
    )


def parse_http_authority(value: str) -> HttpAuthority:
    if (
        not value
        or value != value.strip()
        or not value.isascii()
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(character in value for character in "/\\@?#,")
    ):
        raise RequestAuthorityError(
            "Ugyldig Host-header.",
            status=HTTPStatus.BAD_REQUEST,
        )

    raw_host: str
    raw_port = ""
    if value.startswith("["):
        closing_bracket = value.find("]")
        if closing_bracket < 0:
            raise RequestAuthorityError(
                "Ugyldig Host-header.",
                status=HTTPStatus.BAD_REQUEST,
            )
        raw_host = value[1:closing_bracket]
        remainder = value[closing_bracket + 1 :]
        if remainder:
            if not remainder.startswith(":") or len(remainder) == 1:
                raise RequestAuthorityError(
                    "Ugyldig Host-header.",
                    status=HTTPStatus.BAD_REQUEST,
                )
            raw_port = remainder[1:]
        try:
            parsed_ip = ipaddress.ip_address(raw_host)
        except ValueError as exc:
            raise RequestAuthorityError(
                "Ugyldig Host-header.",
                status=HTTPStatus.BAD_REQUEST,
            ) from exc
        if parsed_ip.version != 6:
            raise RequestAuthorityError(
                "Ugyldig Host-header.",
                status=HTTPStatus.BAD_REQUEST,
            )
        host = str(parsed_ip)
        is_ip_literal = True
    else:
        if "[" in value or "]" in value or value.count(":") > 1:
            raise RequestAuthorityError(
                "Ugyldig Host-header.",
                status=HTTPStatus.BAD_REQUEST,
            )
        raw_host, separator, raw_port = value.partition(":")
        if not separator:
            raw_port = ""
        elif not raw_port:
            raise RequestAuthorityError(
                "Ugyldig Host-header.",
                status=HTTPStatus.BAD_REQUEST,
            )
        if not raw_host:
            raise RequestAuthorityError(
                "Ugyldig Host-header.",
                status=HTTPStatus.BAD_REQUEST,
            )
        host, is_ip_literal = normalize_http_host(raw_host)

    port = parse_http_port(raw_port) if raw_port else None
    return HttpAuthority(host=host, port=port, is_ip_literal=is_ip_literal)


def parse_http_port(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise RequestAuthorityError(
            "Ugyldig port i Host-headeren.",
            status=HTTPStatus.BAD_REQUEST,
        )
    port = int(value)
    if not 1 <= port <= 65535:
        raise RequestAuthorityError(
            "Ugyldig port i Host-headeren.",
            status=HTTPStatus.BAD_REQUEST,
        )
    return port


def normalize_http_host(value: str) -> tuple[str, bool]:
    try:
        return str(ipaddress.ip_address(value)), True
    except ValueError:
        return value.casefold(), False


def request_host_is_allowed(
    authority: HttpAuthority,
    *,
    bind_host: str,
    server_host: str,
) -> bool:
    configured_host, configured_is_ip = normalize_http_host(bind_host)
    actual_host, _actual_is_ip = normalize_http_host(server_host)
    if authority.host == "localhost":
        return True
    if authority.host in {configured_host, actual_host}:
        return True
    if configured_is_ip and ipaddress.ip_address(configured_host).is_unspecified:
        return authority.is_ip_literal
    return False


def validate_request_origin(
    value: str,
    *,
    authority: HttpAuthority,
    server_port: int,
) -> None:
    if (
        not value
        or value != value.strip()
        or not value.isascii()
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise RequestAuthorityError(
            "Ugyldig Origin-header.",
            status=HTTPStatus.BAD_REQUEST,
        )
    try:
        parsed = urllib.parse.urlsplit(value)
        origin_port = parsed.port
    except ValueError as exc:
        raise RequestAuthorityError(
            "Ugyldig Origin-header.",
            status=HTTPStatus.BAD_REQUEST,
        ) from exc
    if (
        parsed.scheme.casefold() != "http"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RequestAuthorityError(
            "Ugyldig Origin-header.",
            status=HTTPStatus.BAD_REQUEST,
        )
    origin_host, _is_ip_literal = normalize_http_host(parsed.hostname)
    if origin_host != authority.host or (origin_port or 80) != server_port:
        raise RequestAuthorityError(
            "Origin-headeren stemmer ikke med Host-headeren.",
            status=HTTPStatus.FORBIDDEN,
        )


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
