"""Build and idempotently upsert CBI metadata after observation writes.

Extends the fleet metadata contract with the vendor provenance and survey
dimension columns declared in scripts/init_db.py, and enforces the one rule that
keeps the architecture honest: `original_publisher` is the Confederation of
British Industry, and the delivery provider is recorded separately.

`survey`, `sector`, `measure` and `category` are stored rather than parsed out of
the identifier, so a query can select "every expected selling-price balance
across the three surveys" without a LIKE pattern over series ids.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.engine import Connection

from scripts.config import COUNTRY_CURRENCY, METADATA_TABLE, SCHEMA_NAME
from scripts.time_series import get_series_aggregates

logger = logging.getLogger(__name__)
_TABLE = f"{SCHEMA_NAME}.{METADATA_TABLE}"
BATCH_SIZE = 500
FREQUENCIES = frozenset(
    {
        "daily",
        "weekly",
        "biweekly",
        "monthly",
        "quarterly",
        "semiannual",
        "annual",
        "decennial",
        "quinquennial",
        "irregular",
    }
)
# UNIT VOCABULARY DIVERGENCE -- declared, not silent.
#
# "balance" is a local extension. The fleet-canonical set (collector_template
# and collector_predictor_template, scripts/metadata.py) is exactly:
#   index, percent, ratio, persons, currency, count, tons, hectares,
#   cubic_meters, megawatt_hours, other
#
# A CBI survey reading is a weighted net balance: the share of respondents
# reporting an increase minus the share reporting a decrease, in percentage
# points on [-100, +100]. It is a difference of two percentages, not a
# percentage of a whole, so "percent" would misstate it -- a reader averaging
# or compounding it as a rate would be wrong. No canonical member carries that
# meaning, and "other" would erase the one property that makes these series
# usable as predictors: their sign and bounded scale.
#
# This extension is therefore held here, visible, until the authority is
# reconciled -- see METHODOLOGY.md, "Unit vocabulary". While it stands, this
# collector is NOT fleet-vocabulary conformant, and must not be reported as
# such. Resolving it is an authority decision, not a code change.
UNITS = frozenset(
    {
        "index",
        "percent",
        "ratio",
        "balance",
        "persons",
        "currency",
        "count",
        "tons",
        "hectares",
        "cubic_meters",
        "megawatt_hours",
        "other",
    }
)
SEASONAL_ADJUSTMENTS = frozenset({"sa", "nsa", "not_applicable", "unknown"})
ECO_GROUPS = frozenset(
    {
        "gdp",
        "activity",
        "industrial_production",
        "production",
        "retail_sales",
        "vehicles",
        "tourism",
        "mining",
        "savings",
        "leading_indicators",
        "consumer_prices",
        "producer_prices",
        "inflation",
        "inflation_expectations",
        "labor",
        "employment",
        "unemployment",
        "wages",
        "trade",
        "balance_of_payments",
        "exchange_rates",
        "external_accounts",
        "central_bank",
        "monetary_aggregates",
        "interest_rates",
        "financial_markets",
        "financial_intermediaries",
        "government_securities",
        "currency_in_circulation",
        "payment_systems",
        "petroleum_fund",
        "public_finance",
        "surveys",
        "consumer_confidence",
        "business_confidence",
        "other",
    }
)

# Vendor provenance is compared like every other metadata field, so a run that
# switched delivery provider updates the row instead of leaving it claiming a
# route that no longer produced the history.
_COMPARABLE_COLUMNS = (
    "name",
    "description",
    "country",
    "frequency",
    "unit",
    "first_observation",
    "last_observation",
    "observation_count",
    "eco_group",
    "source_url",
    "last_publish_date",
)
_COLUMNS = ("series_id", *_COMPARABLE_COLUMNS, "collected_at")
_UPDATE_COLUMNS = tuple(column for column in _COLUMNS if column != "series_id")
_MERGE_DIALECTS = frozenset({"databricks", "postgresql"})
_SELECT_SQL = text(f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE}")


def _batch_parameters(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        f"{column}_{index}": row[column] for index, row in enumerate(rows) for column in _COLUMNS
    }


def _insert_statement(count: int) -> TextClause:
    values = ", ".join(
        "(" + ", ".join(f":{column}_{index}" for column in _COLUMNS) + ")" for index in range(count)
    )
    return text(f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) VALUES {values}")


def _merge_statement(count: int) -> TextClause:
    source = " UNION ALL ".join(
        "SELECT " + ", ".join(f":{column}_{index} AS {column}" for column in _COLUMNS)
        for index in range(count)
    )
    assignments = ", ".join(f"{column} = source.{column}" for column in _UPDATE_COLUMNS)
    return text(
        f"MERGE INTO {_TABLE} AS target USING ({source}) AS source ON target.series_id = source.series_id WHEN MATCHED THEN UPDATE SET {assignments}"
    )


_UPDATE_SQL = text(
    f"UPDATE {_TABLE} SET {', '.join(f'{column}=:{column}' for column in _UPDATE_COLUMNS)} WHERE series_id=:series_id"
)


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    assert isinstance(value, date)
    return value


def validate_catalog(catalog: dict[str, dict[str, Any]]) -> None:
    for series_id, fields in sorted(catalog.items()):
        for key in ("name", "source_url"):
            if not str(fields.get(key, "")).strip():
                raise ValueError(f"{series_id} metadata is missing required field {key!r}")
        if fields["frequency"] not in FREQUENCIES:
            raise ValueError(f"{series_id} has unknown frequency {fields['frequency']!r}")
        if fields["unit"] not in UNITS:
            raise ValueError(f"{series_id} has unknown unit {fields['unit']!r}")
        if fields["eco_group"] not in ECO_GROUPS:
            raise ValueError(f"{series_id} has unknown eco_group {fields['eco_group']!r}")
        if fields.get("country", COUNTRY_CURRENCY) != COUNTRY_CURRENCY:
            raise ValueError(f"{series_id} must carry country {COUNTRY_CURRENCY}")
        if fields["seasonal_adjustment"] not in SEASONAL_ADJUSTMENTS:
            raise ValueError(
                f"{series_id} has unknown seasonal_adjustment {fields['seasonal_adjustment']!r}"
            )
        for key in (
            "original_publisher",
            "delivery_provider",
            "vendor_series_id",
            "vendor_field",
            "survey",
            "sector",
            "measure",
            "category",
            "stance",
            "reference_date_rule",
            "release_rule",
            "revision_policy",
            "license_context",
        ):
            if not str(fields.get(key, "")).strip():
                raise ValueError(f"{series_id} metadata is missing required field {key!r}")
        # The publisher is the economic author, never the route the data took.
        # Recording "Bloomberg" here would silently reattribute a CBI statistic
        # to its distributor, which is the error this whole design exists to
        # prevent.
        if fields["original_publisher"].strip().lower() in {
            "bloomberg",
            "lseg",
            "refinitiv",
            "reuters",
        }:
            raise ValueError(
                f"{series_id}: original_publisher is {fields['original_publisher']!r}. A delivery "
                "provider is not the publisher; record it in delivery_provider instead."
            )


def upsert_metadata(
    conn: Connection, catalog: dict[str, dict[str, Any]], collected_at: datetime
) -> tuple[int, int]:
    validate_catalog(catalog)
    aggregates = get_series_aggregates(conn)
    existing = {
        str(row["series_id"]): dict(row) for row in conn.execute(_SELECT_SQL).mappings().all()
    }
    desired: list[dict[str, Any]] = []
    for series_id, fields in sorted(catalog.items()):
        history = aggregates.get(series_id)
        if history is None:
            logger.warning("%s has no stored observations; skipping metadata", series_id)
            continue
        desired.append(
            {
                "series_id": series_id,
                "name": fields["name"],
                "description": fields.get("description"),
                "country": COUNTRY_CURRENCY,
                "frequency": fields["frequency"],
                "unit": fields["unit"],
                "first_observation": _as_date(history["first_observation"]),
                "last_observation": _as_date(history["last_observation"]),
                "observation_count": int(history["observation_count"]),
                "eco_group": fields["eco_group"],
                "source_url": fields["source_url"],
                "last_publish_date": fields.get("last_publish_date"),
                "collected_at": collected_at,
            }
        )
    inserts = [row for row in desired if row["series_id"] not in existing]
    updates: list[dict[str, Any]] = []
    for row in desired:
        current = existing.get(row["series_id"])
        if current is not None and any(
            _normalize(row[column]) != _normalize(current.get(column))
            for column in _COMPARABLE_COLUMNS
        ):
            updates.append(row)
    for start in range(0, len(inserts), BATCH_SIZE):
        batch = inserts[start : start + BATCH_SIZE]
        conn.execute(_insert_statement(len(batch)), _batch_parameters(batch))
    if updates:
        if conn.dialect.name in _MERGE_DIALECTS:
            for start in range(0, len(updates), BATCH_SIZE):
                batch = updates[start : start + BATCH_SIZE]
                conn.execute(_merge_statement(len(batch)), _batch_parameters(batch))
        else:
            conn.execute(_UPDATE_SQL, updates)
    return len(inserts), len(updates)


def _normalize(value: object) -> object:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return str(value)
