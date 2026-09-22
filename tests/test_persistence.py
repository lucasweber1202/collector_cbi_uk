"""End-to-end persistence: idempotency, vintages, revisions and point-in-time.

These drive the real shipped modules against the real shipped DDL, using an
in-process SQLite database and synthetic vendor responses. The three runs the
fleet's definition of done names — fresh build, unchanged rerun, later revision
— are asserted here rather than described.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

import main
from scripts.availability import (
    FIRST_SEEN,
    OFFICIAL_DATE,
    OFFICIAL_TIMESTAMP,
    attribute_release,
    get_series_as_of,
    upsert_availability,
)
from scripts.config import SCHEMA_NAME
from scripts.extract import CollectedData, assemble
from scripts.metadata import upsert_metadata
from scripts.normalize import VendorRow
from scripts.snapshots import upsert_snapshots
from scripts.vendor_provenance import upsert_vendor_provenance
from scripts.time_series import upsert_time_series
from tests.conftest import (
    bloomberg_response,
    lseg_response,
    make_mapping,
    synthetic_quarters,
    synthetic_rows,
)

SERIES = "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED"
QUARTERLY_SERIES = "CBI_SERVICE_SECTOR_CONSUMER_PRICES_CHARGED_EXPECTED"
REGISTRY = {
    (SERIES, "bloomberg"): make_mapping("bloomberg", SERIES, "TESTCBIDTSPE Index", "PX_LAST"),
    (SERIES, "lseg"): make_mapping("lseg", SERIES, "TESTCBIDTSPE=ECI", "VALUE"),
}


def _run(engine: Engine, data: CollectedData, collected_at: datetime) -> dict[str, int]:
    """Persist one collection exactly as main.collect_source does."""
    with engine.begin() as conn:
        snapshots = upsert_snapshots(conn, data.snapshots)
        result = upsert_time_series(conn, data.observations, collected_at)
        rows = main.availability_rows(data, result, collected_at)
        availability = upsert_availability(conn, rows, collected_at)
        inserted, updated = upsert_metadata(conn, data.catalog, collected_at)
        v_inserted, v_updated = upsert_vendor_provenance(conn, data.catalog, collected_at)
    return {
        "observations": result.new_observations,
        "vintages": result.new_vintages,
        "availability": availability,
        "snapshots": snapshots,
        "metadata_inserted": inserted,
        "metadata_updated": updated,
        "vendor_inserted": v_inserted,
        "vendor_updated": v_updated,
    }


def _collect(rows: list[VendorRow], provider: str = "bloomberg") -> CollectedData:
    builder = bloomberg_response if provider == "bloomberg" else lseg_response
    return assemble({SERIES: builder(rows=rows)}, REGISTRY, provider)


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {SCHEMA_NAME}.{table}")).scalar_one())


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_run_one_inserts_the_full_history(engine: Engine) -> None:
    rows = synthetic_rows(count=6)
    summary = _run(engine, _collect(rows), datetime(2026, 9, 17, 9, tzinfo=UTC))
    assert summary["observations"] == 6
    assert summary["availability"] == 6
    assert summary["snapshots"] == 1
    assert summary["metadata_inserted"] == 1
    assert _count(engine, "time_series") == 6


def test_run_two_unchanged_writes_nothing(engine: Engine) -> None:
    """The fleet's central guarantee: an unchanged rerun is a data no-op."""
    rows = synthetic_rows(count=6)
    _run(engine, _collect(rows), datetime(2026, 9, 17, 9, tzinfo=UTC))
    summary = _run(engine, _collect(rows), datetime(2026, 9, 18, 9, tzinfo=UTC))
    assert summary == {
        "observations": 0,
        "vintages": 0,
        "availability": 0,
        "snapshots": 0,
        "metadata_inserted": 0,
        "metadata_updated": 0,
        "vendor_inserted": 0,
        "vendor_updated": 0,
    }
    assert _count(engine, "time_series") == 6
    assert _count(engine, "source_snapshots") == 1


def test_an_unchanged_rerun_reproduces_the_snapshot_digest(engine: Engine) -> None:
    """Snapshot identity must not depend on when the vendor was asked."""
    rows = synthetic_rows(count=4)
    first = _collect(rows)
    second = _collect(rows)
    assert first.snapshots[0].snapshot_id == second.snapshots[0].snapshot_id


def test_a_new_month_is_one_new_observation(engine: Engine) -> None:
    rows = synthetic_rows(count=6)
    _run(engine, _collect(rows), datetime(2026, 9, 17, 9, tzinfo=UTC))
    summary = _run(engine, _collect(synthetic_rows(count=7)), datetime(2026, 9, 18, 9, tzinfo=UTC))
    assert summary["observations"] == 1
    assert summary["vintages"] == 0
    assert summary["snapshots"] == 1


# ---------------------------------------------------------------------------
# Revisions and vintages
# ---------------------------------------------------------------------------


def test_a_later_day_revision_adds_a_vintage_and_keeps_the_old_one(engine: Engine) -> None:
    rows = synthetic_rows(count=6)
    _run(engine, _collect(rows), datetime(2026, 9, 17, 9, tzinfo=UTC))
    revised = list(rows)
    revised[2] = VendorRow(reference_date=revised[2].reference_date, value=99.0)
    summary = _run(engine, _collect(revised), datetime(2026, 9, 18, 9, tzinfo=UTC))
    assert summary["vintages"] == 1
    assert summary["observations"] == 0
    with engine.connect() as conn:
        stored = conn.execute(
            text(
                f"SELECT value, vintage_date FROM {SCHEMA_NAME}.time_series "
                "WHERE series_id = :series AND reference_date = :reference "
                "ORDER BY vintage_date"
            ),
            {"series": SERIES, "reference": revised[2].reference_date},
        ).all()
    assert len(stored) == 2, "the earlier vintage must be preserved, not overwritten"
    assert float(stored[-1][0]) == 99.0


def test_a_same_day_revision_fails_closed(engine: Engine) -> None:
    """A DATE vintage cannot represent two intraday information sets."""
    rows = synthetic_rows(count=4)
    collected_at = datetime(2026, 9, 17, 9, tzinfo=UTC)
    _run(engine, _collect(rows), collected_at)
    revised = list(rows)
    revised[1] = VendorRow(reference_date=revised[1].reference_date, value=42.0)
    with pytest.raises(RuntimeError, match="same-day revision"):
        _run(engine, _collect(revised), datetime(2026, 9, 17, 17, tzinfo=UTC))


def test_a_revision_is_first_seen_and_never_reuses_the_original_release(engine: Engine) -> None:
    """A 2026 revision of a 2024 month must not appear in a 2024 backtest."""
    rows = [
        VendorRow(date(2024, 1, 1), 4.0, release_timestamp=datetime(2024, 2, 6, 0, 1)),
        VendorRow(date(2024, 2, 1), 6.0, release_timestamp=datetime(2024, 3, 5, 0, 1)),
    ]
    _run(engine, _collect(rows, provider="lseg"), datetime(2026, 9, 17, 9, tzinfo=UTC))
    revised = [
        VendorRow(date(2024, 1, 1), 9.9, release_timestamp=datetime(2024, 2, 6, 0, 1)),
        rows[1],
    ]
    revision_run = datetime(2026, 9, 18, 9, tzinfo=UTC)
    _run(engine, _collect(revised, provider="lseg"), revision_run)
    with engine.connect() as conn:
        bases = conn.execute(
            text(
                f"SELECT vintage_date, availability_basis, available_at "
                f"FROM {SCHEMA_NAME}.availability WHERE series_id = :series "
                "AND reference_date = :reference ORDER BY vintage_date"
            ),
            {"series": SERIES, "reference": date(2024, 1, 1)},
        ).all()
    assert [row[1] for row in bases] == [OFFICIAL_TIMESTAMP, FIRST_SEEN]
    assert bases[1][2].astimezone(UTC) == revision_run


# ---------------------------------------------------------------------------
# Point-in-time
# ---------------------------------------------------------------------------


def test_a_vendor_backfill_is_first_seen_not_backdated(engine: Engine) -> None:
    """Bloomberg serving decades today does not mean we knew it back then."""
    collected_at = datetime(2026, 9, 17, 9, tzinfo=UTC)
    _run(engine, _collect(synthetic_rows(count=6)), collected_at)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT availability_basis, available_at FROM {SCHEMA_NAME}.availability "
                "WHERE series_id = :series"
            ),
            {"series": SERIES},
        ).all()
    assert {row[0] for row in rows} == {FIRST_SEEN}
    assert all(row[1].astimezone(UTC) == collected_at for row in rows)


def test_as_of_before_collection_returns_nothing(engine: Engine) -> None:
    """The look-ahead guarantee, stated as a query."""
    collected_at = datetime(2026, 9, 17, 9, tzinfo=UTC)
    _run(engine, _collect(synthetic_rows(count=6)), collected_at)
    assert get_series_as_of(engine, SERIES, collected_at - timedelta(seconds=1)) == []
    assert len(get_series_as_of(engine, SERIES, collected_at)) == 6


def test_as_of_returns_the_vintage_current_at_that_instant(engine: Engine) -> None:
    """A later revision must not mask what was actually known earlier."""
    rows = synthetic_rows(count=4)
    first_run = datetime(2026, 9, 17, 9, tzinfo=UTC)
    _run(engine, _collect(rows), first_run)
    revised = list(rows)
    revised[0] = VendorRow(reference_date=revised[0].reference_date, value=99.0)
    second_run = datetime(2026, 9, 18, 9, tzinfo=UTC)
    _run(engine, _collect(revised), second_run)
    before = {
        row["reference_date"]: row["value"] for row in get_series_as_of(engine, SERIES, first_run)
    }
    after = {
        row["reference_date"]: row["value"] for row in get_series_as_of(engine, SERIES, second_run)
    }
    assert before[rows[0].reference_date] == rows[0].value
    assert after[rows[0].reference_date] == 99.0


def test_a_provider_reported_instant_beats_first_seen(engine: Engine) -> None:
    rows = [VendorRow(date(2024, 1, 1), 4.0, release_timestamp=datetime(2024, 2, 6, 0, 1))]
    _run(engine, _collect(rows, provider="lseg"), datetime(2026, 9, 17, 9, tzinfo=UTC))
    stored = get_series_as_of(engine, SERIES, datetime(2024, 2, 6, 0, 1, tzinfo=UTC))
    assert len(stored) == 1
    assert stored[0]["availability_basis"] == OFFICIAL_TIMESTAMP
    assert get_series_as_of(engine, SERIES, datetime(2024, 2, 6, 0, 0, tzinfo=UTC)) == []


def test_attribution_prefers_instant_then_date_then_first_seen() -> None:
    reference = date(2024, 6, 1)
    collected = datetime(2026, 9, 17, 9, tzinfo=UTC)
    instant = datetime(2024, 7, 2, 0, 1, tzinfo=UTC)
    assert (
        attribute_release(reference, {reference: instant}, {}, collected)[1] == OFFICIAL_TIMESTAMP
    )
    # A date-only release is stamped 00:01 London, which in July is 23:01 UTC the day before.
    available_at, basis, release_date = attribute_release(
        reference, {}, {reference: date(2024, 7, 2)}, collected
    )
    assert basis == OFFICIAL_DATE
    assert available_at == datetime(2024, 7, 1, 23, 1, tzinfo=UTC)
    assert release_date == date(2024, 7, 2)
    assert attribute_release(reference, {}, {}, collected) == (collected, FIRST_SEEN, None)


def test_a_future_reference_month_is_rejected() -> None:
    future = date(datetime.now(UTC).year + 2, 1, 1)
    with pytest.raises(ValueError, match="future"):
        attribute_release(future, {}, {}, datetime.now(UTC))


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_an_observation_reconstructs_its_full_provenance(engine: Engine) -> None:
    """Publisher, provider, vendor id, retrieval, reference, vintage, availability, hash."""
    _run(engine, _collect(synthetic_rows(count=3)), datetime(2026, 9, 17, 9, tzinfo=UTC))
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT v.original_publisher, s.delivery_provider, s.vendor_series_id, "
                    "       s.vendor_field, s.fetched_at, a.reference_date, a.vintage_date, "
                    "       a.available_at, a.availability_basis, s.snapshot_id "
                    f"FROM {SCHEMA_NAME}.availability a "
                    f"JOIN {SCHEMA_NAME}.source_snapshots s ON s.snapshot_id = a.source_snapshot_id "
                    f"JOIN {SCHEMA_NAME}.vendor_provenance v ON v.series_id = a.series_id "
                    "WHERE a.series_id = :series LIMIT 1"
                ),
                {"series": SERIES},
            )
            .mappings()
            .one()
        )
    assert row["original_publisher"] == "Confederation of British Industry"
    assert row["delivery_provider"] == "bloomberg"
    assert row["vendor_series_id"] == "TESTCBIDTSPE Index"
    assert row["availability_basis"] == FIRST_SEEN
    assert len(row["snapshot_id"]) == 64


def test_switching_provider_does_not_create_a_second_economic_series(engine: Engine) -> None:
    """The database key is economic. Changing route updates provenance, nothing else."""
    rows = synthetic_rows(count=4)
    _run(engine, _collect(rows, provider="bloomberg"), datetime(2026, 9, 17, 9, tzinfo=UTC))
    summary = _run(engine, _collect(rows, provider="lseg"), datetime(2026, 9, 18, 9, tzinfo=UTC))
    with engine.connect() as conn:
        series_ids = (
            conn.execute(text(f"SELECT DISTINCT series_id FROM {SCHEMA_NAME}.time_series"))
            .scalars()
            .all()
        )
        provider = conn.execute(
            text(f"SELECT delivery_provider FROM {SCHEMA_NAME}.vendor_provenance WHERE series_id = :s"),
            {"s": SERIES},
        ).scalar_one()
    assert series_ids == [SERIES]
    assert summary["observations"] == 0, "the same values delivered by another vendor are not new"
    assert summary["metadata_updated"] == 0, (
        "a vendor switch is not an economic change and must not touch metadata"
    )
    assert summary["vendor_updated"] == 1
    assert provider == "lseg"


def test_metadata_refuses_a_delivery_provider_as_publisher(engine: Engine) -> None:
    """The architectural error this design exists to prevent, asserted."""
    data = _collect(synthetic_rows(count=3))
    data.catalog[SERIES]["original_publisher"] = "Bloomberg"
    with engine.begin() as conn:
        upsert_time_series(conn, data.observations, datetime(2026, 9, 17, 9, tzinfo=UTC))
        with pytest.raises(ValueError, match="not the publisher"):
            upsert_metadata(conn, data.catalog, datetime(2026, 9, 17, 9, tzinfo=UTC))


def test_a_cross_check_disagreement_is_surfaced_for_the_run_to_refuse() -> None:
    """main refuses to persist when two licensed routes disagree."""
    data = _collect(synthetic_rows(count=3))
    data.cross_check_differences = [
        "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED 2024-01-01: bloomberg=4.0 lseg=9.0"
    ]
    assert data.cross_check_differences


def test_availability_rows_are_immutable_once_written(engine: Engine) -> None:
    """A later run knowing more must not revise availability downwards."""
    rows = synthetic_rows(count=3)
    data = _collect(rows)
    collected_at = datetime(2026, 9, 17, 9, tzinfo=UTC)
    _run(engine, data, collected_at)
    with engine.begin() as conn:
        result = upsert_time_series(conn, data.observations, collected_at)
        repeat: list[dict[str, Any]] = main.availability_rows(data, result, collected_at)
        assert upsert_availability(conn, repeat, collected_at) == 0


# ---------------------------------------------------------------------------
# Quarterly surveys
# ---------------------------------------------------------------------------


def test_a_quarterly_survey_stores_one_row_per_quarter(engine: Engine) -> None:
    """The Service Sector Survey is quarterly and must not be read as monthly."""
    rows = synthetic_quarters(count=6)
    registry = {
        (QUARTERLY_SERIES, "bloomberg"): make_mapping(
            "bloomberg", QUARTERLY_SERIES, "TESTCBISSCP Index", "PX_LAST"
        ),
        (QUARTERLY_SERIES, "lseg"): make_mapping(
            "lseg", QUARTERLY_SERIES, "TESTCBISSCP=ECI", "VALUE"
        ),
    }
    data = assemble(
        {QUARTERLY_SERIES: bloomberg_response(series_id=QUARTERLY_SERIES, rows=rows)},
        registry,
        "bloomberg",
    )
    summary = _run(engine, data, datetime(2026, 9, 17, 9, tzinfo=UTC))
    assert summary["observations"] == 6
    with engine.connect() as conn:
        stored = (
            conn.execute(
                text(
                    f"SELECT reference_date FROM {SCHEMA_NAME}.time_series ORDER BY reference_date"
                ),
            )
            .scalars()
            .all()
        )
        frequency = conn.execute(
            text(f"SELECT frequency FROM {SCHEMA_NAME}.metadata WHERE series_id = :s"),
            {"s": QUARTERLY_SERIES},
        ).scalar_one()
    assert [_as_stored_date(value).month for value in stored] == [1, 4, 7, 10, 1, 4]
    assert frequency == "quarterly"


def test_the_two_service_sectors_are_separate_stored_series(engine: Engine) -> None:
    """Business/professional and consumer services never merge into one row set."""
    consumer = "CBI_SERVICE_SECTOR_CONSUMER_PRICES_CHARGED_EXPECTED"
    business = "CBI_SERVICE_SECTOR_BUSINESS_PROFESSIONAL_PRICES_CHARGED_EXPECTED"
    rows = synthetic_quarters(count=4)
    registry = {
        (series_id, provider): make_mapping(provider, series_id, f"TEST{index} Index", "PX_LAST")
        for index, series_id in enumerate((consumer, business))
        for provider in ("bloomberg", "lseg")
    }
    responses = {
        consumer: bloomberg_response(series_id=consumer, rows=rows, identifier="TEST0 Index"),
        business: bloomberg_response(series_id=business, rows=rows, identifier="TEST1 Index"),
    }
    _run(engine, assemble(responses, registry, "bloomberg"), datetime(2026, 9, 17, 9, tzinfo=UTC))
    with engine.connect() as conn:
        sectors = conn.execute(
            text(
                f"SELECT series_id, sector FROM {SCHEMA_NAME}.vendor_provenance "
                "ORDER BY series_id"
            )
        ).all()
    assert {row[1] for row in sectors} == {"consumer", "business_professional"}
    assert len(sectors) == 2


def _as_stored_date(value: object) -> date:
    """Coerce a driver-returned reference date for assertion."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    assert isinstance(value, date)
    return value
