from __future__ import annotations


def require_int(value: object, field: str) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise ValueError(f"Ugyldig {field}: forventet heltall.")
    try:
        return int(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Ugyldig {field}: forventet heltall.") from exc


def require_float(value: object, field: str) -> float:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise ValueError(f"Ugyldig {field}: forventet tall.") from None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Ugyldig {field}: forventet tall.") from exc


def optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return require_int(value, field)


def optional_float(value: object, field: str) -> float | None:
    if value is None:
        return None
    return require_float(value, field)
