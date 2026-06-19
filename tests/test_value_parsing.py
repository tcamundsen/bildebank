from __future__ import annotations

import pytest

from bildebank.value_parsing import optional_float, optional_int, require_float, require_int


def test_require_int_accepts_json_and_sqlite_scalar_values() -> None:
    assert require_int(12, "file_id") == 12
    assert require_int("12", "file_id") == 12
    assert require_int(12.9, "file_id") == 12


def test_require_float_accepts_json_and_sqlite_scalar_values() -> None:
    assert require_float(1, "score") == 1.0
    assert require_float("1.25", "score") == 1.25


@pytest.mark.parametrize("value", [None, [], {}, "ikke-et-tall"])
def test_required_values_reject_missing_or_invalid_input(value: object) -> None:
    with pytest.raises(ValueError):
        require_int(value, "file_id")
    with pytest.raises(ValueError):
        require_float(value, "score")


def test_optional_values_accept_none() -> None:
    assert optional_int(None, "file_id") is None
    assert optional_float(None, "score") is None
