"""Provider adapters: response parsing and, above all, failure classification.

Bloomberg's response objects are exercised through a minimal stand-in that
mimics the blpapi Element protocol this collector actually uses — hasElement,
getElement, numValues, getValueAsElement and the typed getters. That is enough
to prove the parser reads the right elements and raises the right exception for
each error shape, without a Terminal and without a licensed value.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from scripts import extract_bloomberg, extract_lseg
from scripts.vendor_errors import (
    EntitlementError,
    FieldNotFoundError,
    IdentifierNotFoundError,
    TemporaryProviderError,
    VendorAuthenticationError,
)
from tests.conftest import make_mapping

MAPPING = make_mapping(
    "bloomberg", "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED", "TESTCBIDTSPE Index", "PX_LAST"
)
QUERY = {"service": "//blp/refdata", "security": "TESTCBIDTSPE Index"}
RETRIEVED = datetime(2026, 9, 17, tzinfo=UTC)


def naive(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build a vendor stamp carrying no zone, as the provider actually sends it.

    The code under test is what decides these mean Europe/London. Stamping them
    UTC here to satisfy DTZ001 would delete the behaviour the tests exist to
    pin, so the zone stays absent and the rule is waived in this one place.
    """
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001


class FakeElement:
    """A minimal stand-in for a blpapi Element."""

    def __init__(
        self, values: dict[str, Any] | None = None, array: list[FakeElement] | None = None
    ):
        self._values = values or {}
        self._array = array or []

    def hasElement(self, name: str) -> bool:
        return name in self._values

    def getElement(self, name: str) -> Any:
        return self._values[name]

    def numValues(self) -> int:
        return len(self._array)

    def getValueAsElement(self, index: int) -> FakeElement:
        return self._array[index]

    def getElementAsString(self, name: str) -> str:
        return str(self._values[name])

    def getElementAsFloat(self, name: str) -> float:
        return float(self._values[name])

    def getElementAsDatetime(self, name: str) -> Any:
        return self._values[name]


def _point(day: date, value: float) -> FakeElement:
    return FakeElement({"date": day, "PX_LAST": value})


def _message(security_data: FakeElement) -> FakeElement:
    return FakeElement({"securityData": security_data})


def _error(subcategory: str, message: str) -> FakeElement:
    return FakeElement({"subcategory": subcategory, "message": message})


# ---------------------------------------------------------------------------
# Bloomberg parsing
# ---------------------------------------------------------------------------


def test_bloomberg_parses_a_historical_response() -> None:
    field_data = FakeElement(array=[_point(date(2024, 1, 31), 4.0), _point(date(2024, 2, 29), 6.0)])
    messages = [_message(FakeElement({"fieldData": field_data}))]
    response = extract_bloomberg.parse_historical_response(messages, MAPPING, QUERY, RETRIEVED)
    assert response.provider == "bloomberg"
    assert response.series_id == "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED"
    assert [(row.reference_date, row.value) for row in response.rows] == [
        (date(2024, 1, 31), 4.0),
        (date(2024, 2, 29), 6.0),
    ]


def test_bloomberg_records_a_skipped_point_as_a_warning() -> None:
    """A point missing the requested field is visible, never silently dropped."""
    field_data = FakeElement(array=[FakeElement({"date": date(2024, 1, 31)})])
    messages = [_message(FakeElement({"fieldData": field_data}))]
    response = extract_bloomberg.parse_historical_response(messages, MAPPING, QUERY, RETRIEVED)
    assert response.rows == ()
    assert response.warnings


@pytest.mark.parametrize(
    ("subcategory", "message", "expected"),
    [
        ("NOT_ENTITLED_TO_SECURITY", "no entitlement", EntitlementError),
        ("NOT_ENTITLED_TO_FIELD", "no field entitlement", EntitlementError),
        ("NOT_LOGGED_IN", "session not logged in", VendorAuthenticationError),
        ("INVALID_SECURITY", "unknown security", IdentifierNotFoundError),
        ("BAD_FLD", "field not applicable", FieldNotFoundError),
        ("SOMETHING_NEW", "unrecognised condition", TemporaryProviderError),
    ],
)
def test_bloomberg_security_errors_map_to_distinct_exceptions(
    subcategory: str, message: str, expected: type[Exception]
) -> None:
    """Entitlement, authentication, bad ticker and bad field are different problems."""
    messages = [_message(FakeElement({"securityError": _error(subcategory, message)}))]
    with pytest.raises(expected):
        extract_bloomberg.parse_historical_response(messages, MAPPING, QUERY, RETRIEVED)


def test_bloomberg_response_error_is_raised_before_any_parsing() -> None:
    messages = [FakeElement({"responseError": _error("NOT_AUTHORIZED", "denied")})]
    with pytest.raises(EntitlementError):
        extract_bloomberg.parse_historical_response(messages, MAPPING, QUERY, RETRIEVED)


def test_bloomberg_field_exception_is_raised_not_skipped() -> None:
    """A rejected field must fail the run, not yield a quietly empty series."""
    exceptions = FakeElement(array=[FakeElement({"errorInfo": _error("BAD_FLD", "invalid field")})])
    messages = [_message(FakeElement({"fieldExceptions": exceptions}))]
    with pytest.raises(FieldNotFoundError):
        extract_bloomberg.parse_historical_response(messages, MAPPING, QUERY, RETRIEVED)


def test_bloomberg_query_records_the_monthly_request_shape() -> None:
    """The query is part of snapshot identity, so it must describe the real request."""
    query = extract_bloomberg.build_query(MAPPING, date(2020, 1, 1), date(2026, 9, 1))
    assert query["periodicitySelection"] == "MONTHLY"
    assert query["security"] == "TESTCBIDTSPE Index"
    assert query["startDate"] == "2020-01-01"


# ---------------------------------------------------------------------------
# LSEG parsing
# ---------------------------------------------------------------------------

LSEG_MAPPING = make_mapping(
    "lseg", "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED", "TESTCBIDTSPE=ECI", "VALUE"
)


def test_lseg_parses_records_including_a_reported_release_instant() -> None:
    records = [
        {"date": "2024-01-31", "VALUE": 4.0, "release_timestamp": "2024-02-06T00:01:00"},
        {"date": "2024-02-29", "VALUE": 6.0},
    ]
    response = extract_lseg.parse_history_records(
        records, LSEG_MAPPING, {"universe": "TESTCBIDTSPE=ECI"}, RETRIEVED
    )
    assert response.provider == "lseg"
    assert response.rows[0].release_timestamp == naive(2024, 2, 6, 0, 1)
    assert response.rows[1].release_timestamp is None


def test_lseg_null_values_survive_as_none_rather_than_zero() -> None:
    """A missing period is missing. Coercing it to 0.0 would invent a zero balance."""
    response = extract_lseg.parse_history_records(
        [{"date": "2024-01-31", "VALUE": None}], LSEG_MAPPING, {}, RETRIEVED
    )
    assert response.rows[0].value is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("User is not entitled to this instrument", EntitlementError),
        ("Access denied for RIC", EntitlementError),
        ("Authentication failed for the session", VendorAuthenticationError),
        ("Invalid RIC provided", IdentifierNotFoundError),
        ("Invalid field requested", FieldNotFoundError),
        ("Request timed out", TemporaryProviderError),
        ("Something nobody has seen before", TemporaryProviderError),
    ],
)
def test_lseg_errors_map_to_distinct_exceptions(message: str, expected: type[Exception]) -> None:
    classified = extract_lseg.classify_error(RuntimeError(message), "TESTCBIDTSPE=ECI", "VALUE")
    assert isinstance(classified, expected)


def test_lseg_field_classification_wins_over_identifier_classification() -> None:
    """'Invalid field' must not be read as 'invalid RIC': they need different fixes."""
    classified = extract_lseg.classify_error(
        RuntimeError("Invalid field for this invalid universe"), "X=ECI", "VALUE"
    )
    assert isinstance(classified, FieldNotFoundError)


class FakeFrame:
    """A minimal stand-in for the DataFrame the library returns."""

    def __init__(self, columns: list[str], index: list[Any], data: dict[str, list[Any]]):
        self.columns = columns
        self.index = index
        self._data = data

    def __getitem__(self, column: str) -> list[Any]:
        return self._data[column]


def test_lseg_reduces_a_single_column_frame_even_when_the_name_differs() -> None:
    frame = FakeFrame(["Prices"], [date(2024, 1, 31)], {"Prices": [4.0]})
    records = extract_lseg.dataframe_to_records(frame, "VALUE")
    assert records == [{"date": date(2024, 1, 31), "VALUE": 4.0}]


def test_lseg_refuses_to_guess_between_several_columns() -> None:
    """Picking a column by position would silently store the wrong statistic."""
    frame = FakeFrame(
        ["Selling prices", "Costs"], [date(2024, 1, 31)], {"Selling prices": [4.0], "Costs": [9.0]}
    )
    with pytest.raises(FieldNotFoundError):
        extract_lseg.dataframe_to_records(frame, "VALUE")


def test_lseg_nan_becomes_none() -> None:
    frame = FakeFrame(["VALUE"], [date(2024, 1, 31)], {"VALUE": [float("nan")]})
    assert extract_lseg.dataframe_to_records(frame, "VALUE")[0]["VALUE"] is None
