"""Shared fixtures and synthetic vendor responses.

Every fixture value here is invented. CBI data is licensed and must never enter
this repository, so the fixtures reproduce the *shape* of a vendor response —
period stamps, a balance-like magnitude, the missing values and the release
timestamps a provider does or does not supply — and nothing of its content. A
test that needed a real CBI balance would be a test that could not be committed.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from scripts import init_db
from scripts.config import SCHEMA_NAME
from scripts.normalize import VendorRow, VendorSeriesResponse
from scripts.vendor_registry import VendorMapping

# sqlite3 removed its implicit datetime adapters in Python 3.12, and the
# collector stores timezone-aware instants in `availability.available_at`.
# Registering the pair here keeps the test database round-tripping the same
# values PostgreSQL and Databricks store, rather than silently stringifying.
sqlite3.register_adapter(datetime, lambda value: value.isoformat())
sqlite3.register_adapter(date, lambda value: value.isoformat())
sqlite3.register_converter(
    "TIMESTAMP", lambda raw: datetime.fromisoformat(raw.decode()).astimezone(UTC)
)
sqlite3.register_converter("DATE", lambda raw: date.fromisoformat(raw.decode()[:10]))


def build_sqlite_engine(tmp_path: Path) -> Engine:
    """Create the shipped tables in an attached SQLite database.

    SQLite has no CREATE SCHEMA, so the schema is attached under its production
    name and every other DDL statement is the one the collector ships.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'main.db'}",
        connect_args={"detect_types": sqlite3.PARSE_DECLTYPES},
    )
    schema_path = tmp_path / "predictors.db"

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        dbapi_connection.execute(f"ATTACH DATABASE '{schema_path}' AS {SCHEMA_NAME}")

    logs = init_db.CREATE_LOGS_TABLE.replace(
        "id BIGINT GENERATED ALWAYS AS IDENTITY", "id INTEGER PRIMARY KEY AUTOINCREMENT"
    ).replace(",\n    CONSTRAINT pk_logs PRIMARY KEY (id)", "")
    with engine.begin() as conn:
        for statement in (
            init_db.CREATE_METADATA_TABLE,
            init_db.CREATE_TIME_SERIES_TABLE.format(double="DOUBLE"),
            init_db.CREATE_AVAILABILITY_TABLE,
            init_db.CREATE_SNAPSHOTS_TABLE,
            init_db.CREATE_VENDOR_PROVENANCE_TABLE,
            logs,
        ):
            conn.execute(text(statement))
    return engine


def build_postgres_engine(url: str) -> Engine:
    """Create the shipped tables in a throwaway schema on a real PostgreSQL.

    The DDL is the collector's own, including CREATE SCHEMA, which SQLite
    cannot execute at all. This is the only way the shipped statements, the
    MERGE path and the TIMESTAMP/DATE round trip get exercised on the engine
    production actually uses.
    """
    engine = create_engine(url)
    double = init_db.double_type("postgresql")
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE"))
        for statement in (
            init_db.CREATE_SCHEMA,
            init_db.CREATE_METADATA_TABLE,
            init_db.CREATE_TIME_SERIES_TABLE.format(double=double),
            init_db.CREATE_AVAILABILITY_TABLE,
            init_db.CREATE_SNAPSHOTS_TABLE,
            init_db.CREATE_VENDOR_PROVENANCE_TABLE,
            init_db.CREATE_LOGS_TABLE,
        ):
            conn.execute(text(statement))
    return engine


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    """Yield a disposable database carrying the shipped DDL.

    Every persistence test runs twice when COLLECTOR_TEST_PG_URL points at a
    PostgreSQL this suite may drop and recreate a schema in. Without it the
    PostgreSQL pass is skipped rather than silently not happening: this
    collector cannot reach its licensed vendor from a test, so a fixture-driven
    run on the real engine is the only database evidence available to it.
    """
    if request.param == "postgresql":
        url = os.getenv("COLLECTOR_TEST_PG_URL")
        if not url:
            pytest.skip("set COLLECTOR_TEST_PG_URL to run the persistence suite on PostgreSQL")
        created = build_postgres_engine(url)
    else:
        created = build_sqlite_engine(tmp_path)
    try:
        yield created
    finally:
        created.dispose()


# ---------------------------------------------------------------------------
# Synthetic vendor responses
# ---------------------------------------------------------------------------

FIRST_MONTH = date(2024, 1, 1)


def synthetic_rows(count: int = 6, start: float = 4.0, step: float = 2.0) -> list[VendorRow]:
    """Build `count` monthly rows with invented, balance-shaped values."""
    rows = []
    for index in range(count):
        month = FIRST_MONTH.replace(year=FIRST_MONTH.year + (index // 12))
        month = month.replace(month=(index % 12) + 1)
        rows.append(VendorRow(reference_date=month, value=round(start + index * step, 4)))
    return rows


def synthetic_quarters(count: int = 6, start: float = 4.0, step: float = 2.0) -> list[VendorRow]:
    """Build `count` quarterly rows stamped at quarter starts."""
    rows = []
    for index in range(count):
        year = FIRST_MONTH.year + (index // 4)
        month = (index % 4) * 3 + 1
        rows.append(
            VendorRow(reference_date=date(year, month, 1), value=round(start + index * step, 4))
        )
    return rows


def bloomberg_response(
    series_id: str = "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED",
    rows: list[VendorRow] | None = None,
    identifier: str = "TESTCBIDTSPE Index",
    field: str = "PX_LAST",
) -> VendorSeriesResponse:
    """A Bloomberg-shaped response carrying invented values."""
    return VendorSeriesResponse(
        provider="bloomberg",
        series_id=series_id,
        identifier=identifier,
        field=field,
        query={"service": "//blp/refdata", "security": identifier, "field": field},
        rows=tuple(synthetic_rows() if rows is None else rows),
        retrieved_at=datetime(2026, 9, 17, 9, 30, tzinfo=UTC),
    )


def lseg_response(
    series_id: str = "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED",
    rows: list[VendorRow] | None = None,
    identifier: str = "TESTCBIDTSPE=ECI",
    field: str = "VALUE",
) -> VendorSeriesResponse:
    """An LSEG-shaped response carrying the same invented values."""
    return VendorSeriesResponse(
        provider="lseg",
        series_id=series_id,
        identifier=identifier,
        field=field,
        query={"library": "lseg.data", "universe": identifier, "fields": field},
        rows=tuple(synthetic_rows() if rows is None else rows),
        retrieved_at=datetime(2026, 9, 17, 9, 31, tzinfo=UTC),
    )


def make_mapping(provider: str, series_id: str, identifier: str, field: str) -> VendorMapping:
    """A resolved registry mapping, for tests that need one without the CSV."""
    return VendorMapping(
        series_id=series_id,
        provider=provider,
        identifier=identifier,
        field=field,
        vendor_description="Synthetic test mapping",
        history_start=FIRST_MONTH,
        confirmed_on=date(2026, 9, 17),
        notes="",
    )
