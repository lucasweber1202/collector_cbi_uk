"""Run the standalone collector_cbi_uk pipeline.

CBI is the economic publisher. Bloomberg and LSEG are licensed delivery
providers. Extraction lives in scripts/extract.py and the provider adapters;
persistence, point-in-time semantics and logging are the fleet's.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import traceback
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.engine import Engine

from scripts.availability import (
    attribute_release,
    refresh_snapshot_provenance,
    upsert_availability,
)
from scripts.config import (
    DEFAULT_START_DATE,
    LOG_LEVEL,
    START_DATE_LOOKBACK_MONTHS,
    missing_environment,
    unresolved_credentials,
)
from scripts.db import build_engine
from scripts.extract import CollectedData, collect, filter_usable_series
from scripts.init_db import init_db
from scripts.metadata import upsert_metadata
from scripts.run_logs import insert_run_log
from scripts.snapshots import upsert_snapshots
from scripts.time_series import WriteResult, get_last_observations, upsert_time_series
from scripts.vendor_provenance import upsert_vendor_provenance

logger = logging.getLogger("main")
TRANSACTIONAL_DIALECTS = frozenset({"postgresql", "sqlite"})


def _setup_logging(level: str) -> io.StringIO:
    buffer = io.StringIO()
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(formatter)
    buffer_handler = logging.StreamHandler(stream=buffer)
    buffer_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.ERROR)
    root.addHandler(stream_handler)
    root.addHandler(buffer_handler)
    logging.getLogger("main").setLevel(level.upper())
    logging.getLogger("scripts").setLevel(level.upper())
    return buffer


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect the CBI survey balances.")
    parser.add_argument("--log-level", default=LOG_LEVEL)
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=None,
        help=(
            "Earliest reference date to process. If omitted, the pipeline "
            "rewinds from the latest stored reference_date."
        ),
    )
    return parser.parse_args(argv)


def _rewind(anchor: date, months: int) -> date:
    """Step ``anchor`` back ``months`` calendar months, clamping the day."""
    total = (anchor.year * 12 + anchor.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(
        anchor.day,
        [
            31,
            29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ][month - 1],
    )
    return date(year, month, day)


def resolve_start_date(engine: Engine, explicit: date | None) -> date:
    """Canonical incremental contract (GUIDELINES.md 5).

    An explicit --start-date always wins. Otherwise rewind
    START_DATE_LOOKBACK_MONTHS from the latest stored reference_date so late
    revisions are re-fetched without re-downloading the archive; on a fresh
    database there is nothing to rewind from, so fall back to
    DEFAULT_START_DATE.
    """
    if explicit is not None:
        logger.info("Start date %s (explicit --start-date)", explicit)
        return explicit
    stored = get_last_observations(engine)
    if not stored:
        logger.info("Start date %s (fresh database, COLLECTOR_START_DATE)", DEFAULT_START_DATE)
        return DEFAULT_START_DATE
    latest = max(stored.values())
    start = _rewind(latest, START_DATE_LOOKBACK_MONTHS)
    logger.info(
        "Start date %s (latest stored reference_date %s rewound %d months)",
        start,
        latest,
        START_DATE_LOOKBACK_MONTHS,
    )
    return start


def availability_rows(
    data: CollectedData, result: WriteResult, collected_at: datetime
) -> list[dict[str, Any]]:
    """Build immutable point-in-time rows for the vintages written in this run.

    A later revision of an already stored period must never reuse the original
    publication instant, so a revised vintage is always `first_seen` at the
    moment this run observed it. A first sighting uses whatever the provider
    reported, and `first_seen` when the provider reported nothing — which is the
    normal outcome for a full-history backfill and is the honest answer.
    """
    snapshot_by_key = {
        (observation.series_id, observation.reference_date): observation.snapshot_id
        for observation in data.observations
    }
    rows: list[dict[str, Any]] = []
    for series_id, reference_date, vintage_date in result.written_keys:
        key = (series_id, reference_date, vintage_date)
        if key in result.revised_keys:
            available_at, basis, release_date = collected_at, "first_seen", None
        else:
            available_at, basis, release_date = attribute_release(
                reference_date,
                data.release_instants.get(series_id, {}),
                data.release_dates.get(series_id, {}),
                collected_at,
            )
        rows.append(
            {
                "series_id": series_id,
                "reference_date": reference_date,
                "vintage_date": vintage_date,
                "release_date": release_date,
                "available_at": available_at,
                "availability_basis": basis,
                "source_snapshot_id": snapshot_by_key[(series_id, reference_date)],
            }
        )
    return rows


def collect_source(engine: Engine, start_date: date) -> None:
    """Collect once and persist the result in a single transaction."""
    data = collect()
    # 5.1: prune dead and history-less series, judged over the full
    # parsed history, before anything is written.
    kept_observations, kept_catalog, _usability = filter_usable_series(
        data.observations, data.catalog, datetime.now(UTC).date()
    )
    # A run that keeps no series persists nothing, so returning normally
    # here would exit 0 and read as a successful collection. That is how a
    # source whose published window is shorter than min_history_years stays
    # silently empty indefinitely. Fail loudly and name the verdict counts,
    # so an empty load is a decision rather than a discovery months later.
    if data.observations and not _usability.kept:
        raise ValueError(
            "Usable-series filter dropped every series "
            f"(stale={len(_usability.stale)} "
            f"short_history={len(_usability.short_history)} "
            f"empty={len(_usability.empty)}). Nothing would be persisted: either "
            "the source stopped publishing usable history, or the configured "
            "thresholds do not match this source's published window."
        )
    # This source publishes a whole file per run and cannot be queried by
    # date, so the incremental window is applied after parsing while the
    # external --start-date contract stays the fleet's.
    data = replace(
        data,
        observations=[o for o in kept_observations if o.reference_date >= start_date],
        catalog=kept_catalog,
    )
    collected_at = datetime.now(UTC)
    if data.cross_check_differences:
        # Two licensed routes to the same CBI statistic disagreeing means one of
        # the two vendor identifiers is wrong. Writing the primary's numbers
        # anyway would bury that behind a plausible history.
        raise RuntimeError(
            "Cross-check provider disagrees with the primary; refusing to persist. "
            + "; ".join(data.cross_check_differences[:20])
        )
    for series_id in data.skipped:
        logger.warning("%s skipped: no confirmed vendor identifier", series_id)
    if engine.dialect.name not in TRANSACTIONAL_DIALECTS:
        logger.warning(
            "%s commits statements independently; interrupted Databricks runs are repaired "
            "idempotently by the next run",
            engine.dialect.name,
        )
    with engine.begin() as conn:
        snapshots_written = upsert_snapshots(conn, data.snapshots)
        result = upsert_time_series(conn, data.observations, collected_at)
        rows = availability_rows(data, result, collected_at)
        availability_written = upsert_availability(conn, rows, collected_at)
        # A same-day revision keeps its availability key, so no new row is
        # owed -- but provenance must stop naming the superseded snapshot.
        refresh_snapshot_provenance(
            conn,
            [
                row
                for row in rows
                if (row["series_id"], row["reference_date"], row["vintage_date"])
                in result.same_day_keys
            ],
        )
        metadata_inserted, metadata_updated = upsert_metadata(conn, data.catalog, collected_at)
        vendor_inserted, vendor_updated = upsert_vendor_provenance(conn, data.catalog, collected_at)
    logger.info(
        "result: new_observations=%d new_vintages=%d availability=%d snapshots=%d "
        "metadata_inserted=%d metadata_updated=%d vendor_inserted=%d vendor_updated=%d skipped_series=%d",
        result.new_observations,
        result.new_vintages,
        availability_written,
        snapshots_written,
        metadata_inserted,
        metadata_updated,
        vendor_inserted,
        vendor_updated,
        len(data.skipped),
    )


def main(start_date_arg: date | None = None) -> int:
    """Validate the environment, then build the schema and collect."""
    missing = missing_environment()
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    for name in unresolved_credentials():
        logger.warning("%s is unset; it must resolve from the runtime context", name)
    engine = build_engine()
    try:
        init_db(engine)
        start_date = resolve_start_date(engine, start_date_arg)
        collect_source(engine, start_date)
    finally:
        engine.dispose()
    return 0


def run(argv: list[str] | None = None) -> int:
    """Run the pipeline and persist exactly one run-log row."""
    args = _parse_args(argv)
    log_buffer = _setup_logging(args.log_level)
    started_at = datetime.now(UTC)
    status = "success"
    traceback_text: str | None = None
    return_code = 0
    try:
        return_code = main(args.start_date)
    except Exception:
        status = "error"
        traceback_text = traceback.format_exc()
        logger.exception("Pipeline failed")
        return_code = 1
    finally:
        finished_at = datetime.now(UTC)
        log_engine = None
        try:
            log_engine = build_engine()
            init_db(log_engine)
            insert_run_log(
                log_engine, started_at, finished_at, status, log_buffer.getvalue(), traceback_text
            )
        except Exception:
            logger.exception("Could not persist run log")
            return_code = 1
        finally:
            if log_engine is not None:
                log_engine.dispose()
    return return_code


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
