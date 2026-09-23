"""The canonical contract: what both providers must converge on."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from scripts.normalize import (
    VendorRow,
    canonical_observations,
    compare_responses,
    is_period_stamp,
    london_midnight_utc,
    normalise_reference_date,
    release_instants,
    snapshot_payload,
    to_utc,
)
from scripts.vendor_errors import EmptyHistoryError
from tests.conftest import bloomberg_response, lseg_response, synthetic_rows

QUARTERLY = "CBI_SERVICE_SECTOR_CONSUMER_PRICES_CHARGED_EXPECTED"


def naive(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build a vendor stamp carrying no zone, as the provider actually sends it.

    The code under test is what decides these mean Europe/London. Stamping them
    UTC here to satisfy DTZ001 would delete the behaviour the tests exist to
    pin, so the zone stays absent and the rule is waived in this one place.
    """
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001


def test_month_stamps_collapse_to_the_first_of_the_month() -> None:
    """A provider may stamp a month at its start, its end, or a datetime inside it."""
    assert normalise_reference_date(date(2024, 3, 1)) == date(2024, 3, 1)
    assert normalise_reference_date(date(2024, 3, 31)) == date(2024, 3, 1)
    assert normalise_reference_date(datetime(2024, 3, 31, 23, 59, tzinfo=UTC)) == date(2024, 3, 1)
    assert normalise_reference_date("2024-03-31T00:00:00Z") == date(2024, 3, 1)


def test_quarter_stamps_collapse_to_the_first_month_of_the_quarter() -> None:
    """Quarter-start and quarter-end conventions describe the same survey quarter."""
    for stamp in (date(2024, 1, 1), date(2024, 3, 31), date(2024, 2, 15)):
        assert normalise_reference_date(stamp, "quarterly") == date(2024, 1, 1)
    assert normalise_reference_date(date(2024, 12, 31), "quarterly") == date(2024, 10, 1)


def test_a_quarterly_series_rejects_an_interior_month_end() -> None:
    """2024-02-29 is a month end, and relabelling it Q1 would invent a quarter."""
    assert is_period_stamp(date(2024, 3, 31), "quarterly")
    assert is_period_stamp(date(2024, 4, 1), "quarterly")
    assert not is_period_stamp(date(2024, 2, 29), "quarterly")
    assert not is_period_stamp(date(2024, 5, 1), "quarterly")
    response = bloomberg_response(
        series_id=QUARTERLY, rows=[VendorRow(reference_date=date(2024, 2, 29), value=5.0)]
    )
    with pytest.raises(ValueError, match="period boundary"):
        canonical_observations(response, "snapshot")


def test_a_quarterly_series_accepts_both_quarter_conventions() -> None:
    """The same quarterly history under either convention is the same series."""
    starts = [VendorRow(date(2024, 1, 1), 5.0), VendorRow(date(2024, 4, 1), 7.0)]
    ends = [VendorRow(date(2024, 3, 31), 5.0), VendorRow(date(2024, 6, 30), 7.0)]
    a = canonical_observations(lseg_response(series_id=QUARTERLY, rows=starts), "snap-a")
    b = canonical_observations(bloomberg_response(series_id=QUARTERLY, rows=ends), "snap-b")
    assert [(o.reference_date, o.value) for o in a] == [(o.reference_date, o.value) for o in b]
    assert [o.reference_date for o in a] == [date(2024, 1, 1), date(2024, 4, 1)]


def test_a_mid_month_stamp_is_rejected_rather_than_truncated() -> None:
    """A monthly series returning 2024-03-14 means the identifier is wrong."""
    response = bloomberg_response(rows=[VendorRow(reference_date=date(2024, 3, 14), value=1.0)])
    with pytest.raises(ValueError, match="period boundary"):
        canonical_observations(response, "snapshot")


def test_an_empty_history_is_an_error_not_an_empty_success() -> None:
    """The failure this collector must never ship: nothing back, reported as fine."""
    response = bloomberg_response(rows=[])
    with pytest.raises(EmptyHistoryError):
        canonical_observations(response, "snapshot")


def test_an_all_null_history_is_also_an_error() -> None:
    """Rows that are all None are indistinguishable from no rows, and fail the same way."""
    rows = [VendorRow(reference_date=date(2024, month, 1), value=None) for month in (1, 2, 3)]
    with pytest.raises(EmptyHistoryError):
        canonical_observations(bloomberg_response(rows=rows), "snapshot")


def test_contradictory_duplicate_months_fail() -> None:
    """Two different values for one month means the response cannot be trusted."""
    rows = [
        VendorRow(reference_date=date(2024, 1, 1), value=1.0),
        VendorRow(reference_date=date(2024, 1, 31), value=2.0),
    ]
    with pytest.raises(ValueError, match="two different values"):
        canonical_observations(bloomberg_response(rows=rows), "snapshot")


def test_consistent_duplicate_months_collapse() -> None:
    """The same month reported twice with the same value is one observation."""
    rows = [
        VendorRow(reference_date=date(2024, 1, 1), value=1.0),
        VendorRow(reference_date=date(2024, 1, 31), value=1.0),
    ]
    observations = canonical_observations(bloomberg_response(rows=rows), "snapshot")
    assert len(observations) == 1


def test_a_non_canonical_series_id_is_refused() -> None:
    """Only ids in the catalog can be stored, whatever the vendor called them."""
    response = bloomberg_response(series_id="BLOOMBERG_CBI_PRICES")
    with pytest.raises(ValueError, match="not a canonical CBI series"):
        canonical_observations(response, "snapshot")


# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------


def test_naive_vendor_timestamps_are_read_as_london_not_utc() -> None:
    """A fixed offset would be wrong for half the year; the tz database is not."""
    winter = to_utc(naive(2024, 1, 15, 0, 1))
    summer = to_utc(naive(2024, 7, 15, 0, 1))
    assert winter.hour == 0 and winter.minute == 1  # GMT == UTC
    assert summer.hour == 23 and summer.day == 14  # BST is UTC+1, so 00:01 is the previous day


def test_release_time_crosses_the_bst_boundary_correctly() -> None:
    """00:01 London on a BST date is 23:01 UTC on the previous date."""
    assert london_midnight_utc(date(2024, 2, 6)) == datetime(2024, 2, 6, 0, 1, tzinfo=UTC)
    assert london_midnight_utc(date(2024, 6, 4)) == datetime(2024, 6, 3, 23, 1, tzinfo=UTC)


def test_aware_vendor_timestamps_are_converted_not_reinterpreted() -> None:
    """A provider that already states a zone is believed, not relabelled."""
    stated = datetime(2024, 7, 15, 12, 0, tzinfo=UTC)
    assert to_utc(stated) == stated


def test_release_instants_omit_months_the_provider_did_not_timestamp() -> None:
    """Absence is recorded as absence, so the caller stamps first_seen instead."""
    rows = [
        VendorRow(date(2024, 1, 1), 1.0, release_timestamp=naive(2024, 2, 6, 0, 1)),
        VendorRow(date(2024, 2, 1), 1.5),
    ]
    instants = release_instants(lseg_response(rows=rows))
    assert set(instants) == {date(2024, 1, 1)}
    assert instants[date(2024, 1, 1)] == datetime(2024, 2, 6, 0, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Cross-provider contract
# ---------------------------------------------------------------------------


def test_both_providers_produce_the_same_canonical_series() -> None:
    """The point of the whole design: the vendor changes, the economic series does not."""
    rows = synthetic_rows()
    from_bloomberg = canonical_observations(bloomberg_response(rows=rows), "snap-a")
    from_lseg = canonical_observations(lseg_response(rows=rows), "snap-b")
    assert [(o.series_id, o.reference_date, o.value) for o in from_bloomberg] == [
        (o.series_id, o.reference_date, o.value) for o in from_lseg
    ]
    assert {o.series_id for o in from_bloomberg} == {
        "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED"
    }


def test_different_month_conventions_still_converge() -> None:
    """Bloomberg month-end and LSEG month-start describe the same survey month."""
    month_start = [VendorRow(date(2024, 1, 1), 2.5), VendorRow(date(2024, 2, 1), 2.7)]
    month_end = [VendorRow(date(2024, 1, 31), 2.5), VendorRow(date(2024, 2, 29), 2.7)]
    a = canonical_observations(lseg_response(rows=month_start), "snap-a")
    b = canonical_observations(bloomberg_response(rows=month_end), "snap-b")
    assert [(o.reference_date, o.value) for o in a] == [(o.reference_date, o.value) for o in b]


def test_cross_check_reports_disagreements_beyond_tolerance() -> None:
    """Two licensed routes to one statistic must not disagree materially."""
    primary = bloomberg_response(rows=[VendorRow(date(2024, 1, 1), 2.50)])
    other = lseg_response(rows=[VendorRow(date(2024, 1, 1), 2.90)])
    assert compare_responses(primary, other, tolerance=0.05)
    assert not compare_responses(
        primary, lseg_response(rows=[VendorRow(date(2024, 1, 1), 2.52)]), tolerance=0.05
    )


def test_cross_check_reports_coverage_gaps() -> None:
    """A provider missing periods is a finding even when the shared ones agree."""
    primary = bloomberg_response(
        rows=[VendorRow(date(2024, 1, 1), 1.0), VendorRow(date(2024, 2, 1), 1.0)]
    )
    other = lseg_response(rows=[VendorRow(date(2024, 1, 1), 1.0)])
    differences = compare_responses(primary, other, tolerance=0.05)
    assert any("only in bloomberg" in difference for difference in differences)


# ---------------------------------------------------------------------------
# Snapshot determinism
# ---------------------------------------------------------------------------


def test_snapshot_payload_excludes_the_retrieval_instant() -> None:
    """Including it would give every rerun a new digest and destroy idempotency."""
    early = bloomberg_response()
    late = bloomberg_response()
    object.__setattr__(late, "retrieved_at", datetime(2030, 1, 1, tzinfo=UTC))
    assert snapshot_payload(early) == snapshot_payload(late)


def test_snapshot_payload_is_row_order_independent() -> None:
    """The same history returned in a different order is the same artifact."""
    rows = synthetic_rows()
    assert snapshot_payload(bloomberg_response(rows=rows)) == snapshot_payload(
        bloomberg_response(rows=list(reversed(rows)))
    )


def test_snapshot_payload_distinguishes_provider_and_window() -> None:
    """A different route or a different query is a different artifact."""
    assert snapshot_payload(bloomberg_response()) != snapshot_payload(lseg_response())
