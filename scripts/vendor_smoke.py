"""Certify one licensed delivery provider end to end, on an entitled machine.

    python -m scripts.vendor_smoke --provider bloomberg

The steps, in order, are the ones a live certification has to prove:

1. authenticate against the provider;
2. fetch a single canonical series;
3. fetch a small window of it;
4. validate the canonical schema;
5. report the latest value;
6. persist it;
7. rerun;
8. confirm the rerun wrote nothing.

Step 8 is the one that matters most and is the easiest to skip. A collector that
passes steps 1-7 and writes duplicate rows on every subsequent run has not been
certified; it has been demonstrated once.

This command refuses to run against unconfirmed identifiers, and it does not
print stored values beyond the single latest one needed to eyeball the result
against the published release. CBI data is licensed and terminal output ends up
in tickets and screenshots.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime

from sqlalchemy.engine import Engine

from scripts.availability import upsert_availability
from scripts.config import PROVIDERS, SCHEMA_NAME
from scripts.db import build_engine
from scripts.extract import CollectedData, assemble, fetch_provider
from scripts.init_db import init_db
from scripts.metadata import upsert_metadata
from scripts.normalize import canonical_observations
from scripts.snapshots import upsert_snapshots
from scripts.time_series import upsert_time_series
from scripts.vendor_errors import PendingVendorDiscoveryError, VendorError
from scripts.vendor_registry import load_registry, resolved_for

logger = logging.getLogger("vendor_smoke")
SMOKE_WINDOW_MONTHS = 13


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the smoke-test command line."""
    parser = argparse.ArgumentParser(
        description="Live certification of one licensed delivery provider."
    )
    parser.add_argument("--provider", required=True, choices=list(PROVIDERS))
    parser.add_argument(
        "--series",
        help="Canonical series to certify. Defaults to the first resolved one.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Also run steps 6-8 against the configured database.",
    )
    return parser.parse_args(argv)


def smoke_window(today: date | None = None) -> tuple[date, date]:
    """Return a deliberately small window: about a year back from today."""
    end = today or datetime.now(UTC).date()
    start_year = end.year - (SMOKE_WINDOW_MONTHS // 12)
    return date(start_year, end.month, 1), end


def run(argv: list[str] | None = None) -> int:  # pragma: no cover - requires a live vendor
    """Certify the provider and report each step's outcome."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    registry = load_registry()
    resolved = resolved_for(args.provider, registry)
    if not resolved:
        raise PendingVendorDiscoveryError(args.provider, "every canonical series")
    mappings = (
        [mapping for mapping in resolved if mapping.series_id == args.series]
        if args.series
        else resolved[:1]
    )
    if not mappings:
        raise PendingVendorDiscoveryError(args.provider, args.series or "")
    mapping = mappings[0]
    start, end = smoke_window()

    try:
        logger.info(
            "1-3. authenticating and fetching %s over %s..%s", mapping.series_id, start, end
        )
        responses = fetch_provider(args.provider, [mapping], start, end)
        response = responses[mapping.series_id]

        logger.info("4. validating the canonical schema")
        observations = canonical_observations(response, "smoke")
        latest = max(observations, key=lambda observation: observation.reference_date)
        logger.info(
            "5. latest: %s %s = %s (check this against the published CBI release)",
            latest.series_id,
            latest.reference_date,
            latest.value,
        )

        if not args.persist:
            logger.info("6-8. skipped; pass --persist to certify the database round trip")
            logger.info("LIVE_VENDOR_SMOKE = PARTIAL (fetch certified, persistence not run)")
            return 0

        engine = build_engine()
        try:
            init_db(engine)
            logger.info("6. persisting into %s", SCHEMA_NAME)
            first = _persist(engine, assemble(responses, registry, args.provider))
            logger.info("7. rerunning against the same vendor history")
            second = _persist(engine, assemble(responses, registry, args.provider))
        finally:
            engine.dispose()

        logger.info("8. rerun wrote: %s", second)
        if any(second.values()):
            logger.error("LIVE_VENDOR_SMOKE = FAIL (the rerun was not a no-op)")
            return 1
        logger.info("first run wrote: %s", first)
        logger.info("LIVE_VENDOR_SMOKE = PASS")
        return 0
    except VendorError as error:
        logger.error("%s", error)
        logger.error("LIVE_VENDOR_SMOKE = FAIL")
        return 1


def _persist(engine: Engine, data: CollectedData) -> dict[str, int]:  # pragma: no cover - live
    """Persist one collection and report exactly what it wrote.

    `main` is imported here rather than at module scope so this module stays
    importable while main is being exercised, and because the smoke test is the
    only caller that needs the pipeline's availability attribution.
    """
    import main

    collected_at = datetime.now(UTC)
    with engine.begin() as conn:
        snapshots = upsert_snapshots(conn, data.snapshots)
        result = upsert_time_series(conn, data.observations, collected_at)
        rows = main.availability_rows(data, result, collected_at)
        availability = upsert_availability(conn, rows, collected_at)
        inserted, updated = upsert_metadata(conn, data.catalog, collected_at)
    return {
        "observations": result.new_observations,
        "vintages": result.new_vintages,
        "availability": availability,
        "snapshots": snapshots,
        "metadata_inserted": inserted,
        "metadata_updated": updated,
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run(sys.argv[1:]))
