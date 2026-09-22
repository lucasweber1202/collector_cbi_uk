"""Point-in-time availability: when each stored vintage actually became knowable.

`time_series.vintage_date` cannot answer that question on its own. A historical
backfill stamps every row with the collection day, so a naive as-of query over
`time_series` would report that twenty years of history appeared at once. This
module stores, per observation vintage, the instant the information was
available from the source and — just as importantly — how strong the evidence
for that instant is.

`availability_basis` is a closed vocabulary, ordered here from strongest to
weakest evidence:

``official_timestamp``
    The delivery provider reported a machine-readable publication instant for
    this observation. Only a provider-reported instant qualifies; a release rule
    read off CBI's website does not, however reliable it looks.
``official_date``
    A publication date was reported, but only to day precision. It is stamped at
    00:01 Europe/London, the earliest defensible instant on a known release day,
    converted to UTC.
``archived_release``
    Recovered from an archived copy of the release rather than the live vendor.
``first_seen``
    Not published with a release date, but this collector itself witnessed the
    observation appear between two runs, so `available_at` is an upper bound.
``inferred``
    Derived from the publisher's documented release rule rather than observed.
    This collector never *produces* `inferred` for vendor-delivered history: a
    vendor backfill that carries no publication instant is `first_seen`, not a
    reconstruction, and the basis is reserved for rows loaded by other means.
``unknown``
    No defensible basis exists.

Only the first three are evidence from the source. `first_seen` is a true upper
bound produced by this collector. `inferred` and `unknown` are NOT point-in-time
guarantees, and `get_series_as_of` refuses them by default so they cannot be
treated as such by accident.

The point this module exists to defend, for a vendor-delivered series: Bloomberg
and LSEG will happily serve decades of CBI survey history in one call today. That
history is real, but the *knowledge* of it is not retroactive. Nothing here ever
backdates an observation to the month it describes. The first run of this
collector stamps the whole backfill `first_seen` at the moment it ran, which is
an honest upper bound, and only months the collector actually watched appear —
or months the provider itself timestamped — can do better.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from scripts.config import AVAILABILITY_TABLE, SCHEMA_NAME, TIME_SERIES_TABLE
from scripts.normalize import london_midnight_utc

logger = logging.getLogger(__name__)
_TABLE = f"{SCHEMA_NAME}.{AVAILABILITY_TABLE}"
_TIME_SERIES = f"{SCHEMA_NAME}.{TIME_SERIES_TABLE}"

OFFICIAL_TIMESTAMP = "official_timestamp"
OFFICIAL_DATE = "official_date"
ARCHIVED_RELEASE = "archived_release"
FIRST_SEEN = "first_seen"
INFERRED = "inferred"
UNKNOWN = "unknown"

AVAILABILITY_BASES = frozenset(
    {OFFICIAL_TIMESTAMP, OFFICIAL_DATE, ARCHIVED_RELEASE, FIRST_SEEN, INFERRED, UNKNOWN}
)

# Bases that constitute evidence the observation was genuinely available at the
# recorded instant. `inferred` and `unknown` are deliberately absent: they are
# reconstructions, and a research query that silently included them would report
# a look-ahead-free history it cannot actually support.
POINT_IN_TIME_BASES = frozenset({OFFICIAL_TIMESTAMP, OFFICIAL_DATE, ARCHIVED_RELEASE, FIRST_SEEN})

_COLUMNS = (
    "series_id",
    "reference_date",
    "vintage_date",
    "release_date",
    "available_at",
    "availability_basis",
    "source_snapshot_id",
    "collected_at",
)
_KEY_COLUMNS = ("series_id", "reference_date", "vintage_date")
BATCH_SIZE = 500


def attribute_release(
    reference_date: date,
    release_instants: dict[date, datetime],
    release_dates: dict[date, date],
    collected_at: datetime,
) -> tuple[datetime, str, date | None]:
    """Decide when one reference month's vintage actually became knowable.

    The ordering is strictly by strength of evidence, and there is no fallback
    that invents one:

    1. The provider reported a publication instant for this month. That is
       `official_timestamp` and is used as reported, in UTC.
    2. The provider reported a publication date only. That is `official_date`,
       stamped at 00:01 Europe/London and converted to UTC through the tz
       database, so GMT and BST both come out right.
    3. Neither. The only defensible instant left is when this collector saw the
       value, so the vintage is `first_seen` at `collected_at`.

    Case 3 is the normal case for a first full-history backfill, and that is the
    correct answer rather than a weakness to be papered over: the desk genuinely
    does not know when a 2011 balance reached the vendor, and claiming to
    know would put a look-ahead into every backtest that ran over it.
    """
    if reference_date > datetime.now(UTC).date():
        raise ValueError(f"Reference date {reference_date} is in the future")
    instant = release_instants.get(reference_date)
    if instant is not None:
        return instant.astimezone(UTC), OFFICIAL_TIMESTAMP, instant.astimezone(UTC).date()
    release_day = release_dates.get(reference_date)
    if release_day is not None:
        return london_midnight_utc(release_day), OFFICIAL_DATE, release_day
    return collected_at, FIRST_SEEN, None


def _insert_statement(count: int) -> Any:
    """Build one multi-row INSERT covering ``count`` rows."""
    values = ", ".join(
        "(" + ", ".join(f":{column}_{index}" for column in _COLUMNS) + ")" for index in range(count)
    )
    return text(f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) VALUES {values}")


def _batch_parameters(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a batch into named parameters suffixed by row position."""
    return {
        f"{column}_{index}": row[column] for index, row in enumerate(rows) for column in _COLUMNS
    }


_EXISTING_SQL = text(
    f"SELECT series_id, reference_date, vintage_date FROM {_TABLE} WHERE series_id IN :series_ids"
).bindparams(bindparam("series_ids", expanding=True))


def upsert_availability(
    conn: Connection, rows: list[dict[str, Any]], collected_at: datetime
) -> int:
    """Insert availability rows for vintages that do not have one yet.

    An availability row is immutable once written. Rewriting it would rewrite
    history: the whole point of the table is that it records what was knowable,
    and a later run knowing more must not revise that downwards.
    """
    if not rows:
        return 0
    for row in rows:
        if row["availability_basis"] not in AVAILABILITY_BASES:
            raise ValueError(f"Unknown availability_basis {row['availability_basis']!r}")
    series_ids = sorted({str(row["series_id"]) for row in rows})
    existing: set[tuple[str, date, date]] = set()
    for start in range(0, len(series_ids), 50):
        for found in conn.execute(
            _EXISTING_SQL, {"series_ids": series_ids[start : start + 50]}
        ).mappings():
            existing.add(
                (
                    str(found["series_id"]),
                    _as_date(found["reference_date"]),
                    _as_date(found["vintage_date"]),
                )
            )
    pending = [
        {**row, "collected_at": collected_at}
        for row in rows
        if (row["series_id"], row["reference_date"], row["vintage_date"]) not in existing
    ]
    if not pending:
        logger.info("Availability: all %d vintages already recorded", len(rows))
        return 0
    logger.info("Availability: writing %d rows in batches of %d", len(pending), BATCH_SIZE)
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        conn.execute(_insert_statement(len(batch)), _batch_parameters(batch))
        logger.info(
            "Availability progress: %d/%d rows",
            min(start + BATCH_SIZE, len(pending)),
            len(pending),
        )
    return len(pending)


def _as_date(value: object) -> date:
    """Coerce a driver-returned date/datetime/string to a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    assert isinstance(value, date)
    return value


def _as_datetime(value: object) -> datetime:
    """Coerce a driver-returned timestamp to a timezone-aware datetime."""
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"Cannot read {value!r} as a timestamp")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_AS_OF_SQL = text(
    f"""SELECT series_id, reference_date, vintage_date, value, available_at, availability_basis
    FROM (
        SELECT ts.series_id AS series_id, ts.reference_date AS reference_date,
               ts.vintage_date AS vintage_date, ts.value AS value,
               av.available_at AS available_at, av.availability_basis AS availability_basis,
               ROW_NUMBER() OVER (
                   PARTITION BY ts.series_id, ts.reference_date
                   ORDER BY ts.vintage_date DESC, ts.collected_at DESC
               ) AS rn
        FROM {_TIME_SERIES} ts
        JOIN {_TABLE} av
          ON av.series_id = ts.series_id
         AND av.reference_date = ts.reference_date
         AND av.vintage_date = ts.vintage_date
        WHERE ts.series_id = :series_id
          AND av.available_at <= :as_of
          AND av.availability_basis IN :bases
    ) ranked
    WHERE rn = 1
    ORDER BY reference_date"""
).bindparams(bindparam("bases", expanding=True))


def get_series_as_of(
    engine: Engine,
    series_id: str,
    as_of: datetime,
    bases: frozenset[str] = POINT_IN_TIME_BASES,
) -> list[dict[str, Any]]:
    """Return the information set for ``series_id`` as it stood at ``as_of``.

    The contract this function exists to keep: no returned row may have
    ``available_at > as_of``. The filter is applied before the latest-vintage
    ranking, so a later revision of a period does not leak in and mask the
    vintage that was actually current at ``as_of``.

    ``bases`` defaults to evidence-backed bases only. Passing a wider set opts
    in explicitly to reconstructed availability, which is not point-in-time.
    """
    unknown = set(bases) - AVAILABILITY_BASES
    if unknown:
        raise ValueError(f"Unknown availability bases requested: {sorted(unknown)}")
    if not bases:
        return []
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                _AS_OF_SQL,
                {"series_id": series_id, "as_of": as_of, "bases": sorted(bases)},
            )
            .mappings()
            .all()
        )
    result = [
        {
            "series_id": str(row["series_id"]),
            "reference_date": _as_date(row["reference_date"]),
            "vintage_date": _as_date(row["vintage_date"]),
            "value": float(row["value"]),
            "available_at": _as_datetime(row["available_at"]),
            "availability_basis": str(row["availability_basis"]),
        }
        for row in rows
    ]
    # Defence in depth. Some drivers compare naive and aware timestamps
    # loosely, and a silent look-ahead is the one failure this module must
    # never ship, so the contract is re-checked in Python before returning.
    leaked = [row for row in result if _as_datetime(row["available_at"]) > as_of]
    if leaked:
        raise RuntimeError(
            f"Look-ahead detected for {series_id}: {len(leaked)} rows with available_at > {as_of}"
        )
    return result


_REFRESH_SNAPSHOT_SQL = text(
    f"""UPDATE {_TABLE} SET source_snapshot_id = :source_snapshot_id
WHERE series_id = :series_id AND reference_date = :reference_date
AND vintage_date = :vintage_date"""
)


def refresh_snapshot_provenance(conn: Connection, rows: list[dict[str, Any]]) -> int:
    """Repoint a same-day-revised vintage at the snapshot that now explains it.

    A same-day revision overwrites today's ``time_series`` row in place, so the
    availability key is unchanged and no new row is owed. But its
    ``source_snapshot_id`` would still name the earlier file of the same day,
    which no longer explains the stored value.

    Only provenance moves. ``available_at``, ``availability_basis`` and
    ``release_date`` are the point-in-time guarantees and stay immutable: the
    observation genuinely did first become knowable when this collector first
    saw it today, and a later revision on the same day does not change that.
    """
    if not rows:
        return 0
    total = len(rows)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    logger.info(
        "Availability: repointing %d same-day vintages in %d batches of %d",
        total,
        total_batches,
        BATCH_SIZE,
    )
    written = 0
    for index, start in enumerate(range(0, total, BATCH_SIZE), start=1):
        batch = rows[start : start + BATCH_SIZE]
        conn.execute(_REFRESH_SNAPSHOT_SQL, batch)
        written += len(batch)
        logger.info(
            "Availability provenance batch %d/%d (%d/%d rows)",
            index,
            total_batches,
            written,
            total,
        )
    return total
