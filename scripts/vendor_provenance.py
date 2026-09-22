"""Vendor delivery provenance — sidecar to the fleet ``metadata`` table.

The canonical ``metadata`` table is fleet-wide and carries no source-specific
columns (GUIDELINES.md §3, §11). BRC/CBI series nevertheless reach us through a
licensed delivery provider, and the provider identifiers, licence context and
publication rules must stay auditable: the economic publisher is the survey
body, never the vendor. Those fields therefore live here, keyed one-to-one on
``series_id``, instead of widening the standardized table.

Idempotent: an unchanged row is neither inserted nor updated.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.engine import Connection

from scripts.config import SCHEMA_NAME, VENDOR_PROVENANCE_TABLE

logger = logging.getLogger(__name__)

_TABLE = f"{SCHEMA_NAME}.{VENDOR_PROVENANCE_TABLE}"
BATCH_SIZE = 500

VENDOR_COLUMNS = (
    "seasonal_adjustment",
    "original_publisher",
    "delivery_provider",
    "vendor_series_id",
    "vendor_field",
    "vendor_description",
    "survey",
    "sector",
    "measure",
    "category",
    "stance",
    "reference_date_rule",
    "release_rule",
    "revision_policy",
    "license_context",
    "history_start",
)
_COLUMNS = ("series_id", *VENDOR_COLUMNS, "collected_at")
_UPDATE_COLUMNS = tuple(column for column in _COLUMNS if column != "series_id")
_MERGE_DIALECTS = frozenset({"databricks", "postgresql"})
_SELECT_SQL = text(f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE}")
_UPDATE_SQL = text(
    f"UPDATE {_TABLE} SET "
    f"{', '.join(f'{column}=:{column}' for column in _UPDATE_COLUMNS)} "
    f"WHERE series_id=:series_id"
)


def _batch_parameters(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        f"{column}_{index}": row[column]
        for index, row in enumerate(rows)
        for column in _COLUMNS
    }


def _insert_statement(count: int) -> TextClause:
    values = ", ".join(
        "(" + ", ".join(f":{column}_{index}" for column in _COLUMNS) + ")"
        for index in range(count)
    )
    return text(f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) VALUES {values}")


def _merge_statement(count: int) -> TextClause:
    source = " UNION ALL ".join(
        "SELECT " + ", ".join(f":{column}_{index} AS {column}" for column in _COLUMNS)
        for index in range(count)
    )
    assignments = ", ".join(f"{column} = source.{column}" for column in _UPDATE_COLUMNS)
    return text(
        f"MERGE INTO {_TABLE} AS target USING ({source}) AS source "
        f"ON target.series_id = source.series_id "
        f"WHEN MATCHED THEN UPDATE SET {assignments}"
    )


def _normalize(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def upsert_vendor_provenance(
    conn: Connection, catalog: dict[str, dict[str, Any]], collected_at: datetime
) -> tuple[int, int]:
    """Upsert one provenance row per catalogued series. Returns (inserted, updated)."""
    existing = {
        str(row["series_id"]): dict(row)
        for row in conn.execute(_SELECT_SQL).mappings().all()
    }
    desired = [
        {
            "series_id": series_id,
            **{column: fields.get(column) for column in VENDOR_COLUMNS},
            "collected_at": collected_at,
        }
        for series_id, fields in sorted(catalog.items())
    ]

    inserts = [row for row in desired if row["series_id"] not in existing]
    updates = [
        row
        for row in desired
        if (current := existing.get(row["series_id"])) is not None
        and any(
            _normalize(row[column]) != _normalize(current.get(column))
            for column in VENDOR_COLUMNS
        )
    ]

    if inserts:
        total_batches = (len(inserts) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info(
            "Inserting %d vendor provenance rows in %d batches of %d",
            len(inserts), total_batches, BATCH_SIZE,
        )
        done = 0
        for index, start in enumerate(range(0, len(inserts), BATCH_SIZE), start=1):
            batch = inserts[start : start + BATCH_SIZE]
            conn.execute(_insert_statement(len(batch)), _batch_parameters(batch))
            done += len(batch)
            logger.info(
                "Inserted batch %d/%d (%d/%d rows)",
                index, total_batches, done, len(inserts),
            )

    if updates:
        total_batches = (len(updates) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info(
            "Updating %d vendor provenance rows in %d batches of %d",
            len(updates), total_batches, BATCH_SIZE,
        )
        done = 0
        if conn.dialect.name in _MERGE_DIALECTS:
            for index, start in enumerate(range(0, len(updates), BATCH_SIZE), start=1):
                batch = updates[start : start + BATCH_SIZE]
                conn.execute(_merge_statement(len(batch)), _batch_parameters(batch))
                done += len(batch)
                logger.info(
                    "Updated batch %d/%d (%d/%d rows)",
                    index, total_batches, done, len(updates),
                )
        else:
            conn.execute(_UPDATE_SQL, updates)

    logger.info(
        "Vendor provenance upsert: inserted=%d updated=%d", len(inserts), len(updates)
    )
    return len(inserts), len(updates)
