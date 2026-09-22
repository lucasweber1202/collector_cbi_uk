"""Collect the CBI survey balances through a licensed delivery provider.

This is the only module that knows a run can involve more than one vendor, and
it keeps three roles strictly apart:

primary
    Supplies the canonical history. Every stored value comes from here.
fallback
    Used only when the primary is *unreachable* — the library is missing, the
    Terminal or Workspace is not running, the provider timed out. An entitlement
    failure or an unknown identifier is never failed over, because both mean the
    configuration is wrong and falling back would hide that behind a different
    vendor's data.
cross-check
    Read and compared, never written. Disagreements are logged and, past the
    configured tolerance, fail the run. The canonical value still comes from the
    primary, so which provider happened to answer can never change the stored
    economic history.

The canonical `series_id` is economic and identical across providers. Provider
identity lives in `source_snapshots` and in the vendor columns of `metadata`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from scripts.config import (
    BLOOMBERG,
    CROSS_CHECK_PROVIDER,
    CROSS_CHECK_TOLERANCE,
    DATA_PROVIDER,
    FALLBACK_PROVIDER,
    HISTORY_START,
    LSEG,
    MAX_STALE_MONTHS,
    MIN_HISTORY_YEARS,
    PROVIDERS,
)
from scripts.normalize import (
    VendorSeriesResponse,
    canonical_observations,
    compare_responses,
    release_instants,
)

# -- series_id contract (GUIDELINES.md 4, 9.2) ---------------------------
# The grammar lives with the extractor that owns it; it is re-exported here
# so the canonical public surface is scripts/extract.py as the guideline
# requires. That parser returns the raw underscore segments, so rejoining
# them is its exact inverse and build(*parse(sid)) == sid.
from scripts.series_catalog import (
    SERIES_BY_ID,
    SeriesDefinition,
    parse_series_id,  # noqa: F401
)
from scripts.snapshots import Snapshot, build_snapshot
from scripts.time_series import Observation
from scripts.vendor_errors import (
    PendingVendorDiscoveryError,
    ProviderUnavailableError,
    TemporaryProviderError,
    VendorError,
)
from scripts.vendor_registry import VendorMapping, load_registry, pending_for, resolved_for


def build_series_id(*components: str) -> str:
    """Rejoin the tuple parse_series_id returned into the original id."""
    if not components:
        raise ValueError("series_id needs at least one component")
    if any(not c or c != c.upper() for c in components):
        raise ValueError(f"invalid series_id components: {components!r}")
    return "_".join(components)


# -- 5.1 usable-series filtering ------------------------------------------


@dataclass(frozen=True)
class UsabilityReport:
    """What the filter removed, for logging and for tests to assert on."""

    kept: tuple[str, ...]
    stale: tuple[str, ...]
    short_history: tuple[str, ...]
    empty: tuple[str, ...]

    @property
    def dropped(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.stale) | set(self.short_history) | set(self.empty)))


def _months_between(earlier: date, later: date) -> int:
    """Whole months from ``earlier`` to ``later``, day-of-month aware."""
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return months


def _is_valid(value: Any) -> bool:
    """A real observation: present, numeric and finite."""
    if value is None:
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric)


def assess_series(
    reference_dates: list[date],
    today: date,
    max_stale_months: int = MAX_STALE_MONTHS,
    min_history_years: float = MIN_HISTORY_YEARS,
) -> str:
    """Classify one series from the reference dates of its valid observations.

    Returns ``"keep"``, ``"empty"``, ``"stale"`` or ``"short_history"``.
    Recency is judged at the period end and over non-null values only: a source
    that keeps listing a discontinued series with empty recent cells must not
    look live because of those blanks.
    """
    if not reference_dates:
        return "empty"
    first, last = min(reference_dates), max(reference_dates)
    if _months_between(last, today) > max_stale_months:
        return "stale"
    if _months_between(first, last) < round(min_history_years * 12):
        return "short_history"
    return "keep"


def filter_usable_series(
    observations: list[Any],
    catalog: dict[str, dict[str, Any]],
    today: date,
    max_stale_months: int = MAX_STALE_MONTHS,
    min_history_years: float = MIN_HISTORY_YEARS,
) -> tuple[list[Any], dict[str, dict[str, Any]], UsabilityReport]:
    """Drop obsolete and history-less series before anything is persisted.

    Runs after parsing and before the time_series / metadata upsert, so the
    standardized tables never carry a dead or stub series, and prunes the
    catalog alongside the observations so metadata can never describe a series
    the database does not hold (GUIDELINES.md 5.1).
    """
    valid_dates: dict[str, list[date]] = {}
    for observation in observations:
        if _is_valid(observation.value):
            valid_dates.setdefault(observation.series_id, []).append(observation.reference_date)

    verdicts: dict[str, str] = {}
    for series_id in set(catalog) | {o.series_id for o in observations}:
        verdicts[series_id] = assess_series(
            valid_dates.get(series_id, []), today, max_stale_months, min_history_years
        )

    keep = {series_id for series_id, verdict in verdicts.items() if verdict == "keep"}
    report = UsabilityReport(
        kept=tuple(sorted(keep)),
        stale=tuple(sorted(s for s, v in verdicts.items() if v == "stale")),
        short_history=tuple(sorted(s for s, v in verdicts.items() if v == "short_history")),
        empty=tuple(sorted(s for s, v in verdicts.items() if v == "empty")),
    )

    if report.dropped:
        logger.info(
            "Usable-series filter: kept %d, dropped %d "
            "(stale=%d short_history=%d empty=%d; max_stale_months=%d min_history_years=%s)",
            len(report.kept),
            len(report.dropped),
            len(report.stale),
            len(report.short_history),
            len(report.empty),
            max_stale_months,
            min_history_years,
        )
        for series_id in report.stale:
            logger.info(
                "Dropped %s: last valid observation older than %d months",
                series_id,
                max_stale_months,
            )
        for series_id in report.short_history:
            logger.info(
                "Dropped %s: valid history shorter than %s years", series_id, min_history_years
            )
        for series_id in report.empty:
            logger.info("Dropped %s: no valid observations", series_id)
    else:
        logger.info("Usable-series filter: all %d series usable", len(report.kept))

    kept_observations = [o for o in observations if o.series_id in keep]
    kept_catalog = {sid: fields for sid, fields in catalog.items() if sid in keep}
    return kept_observations, kept_catalog, report


logger = logging.getLogger(__name__)


@dataclass
class CollectedData:
    """Everything one run produces, before any of it is persisted."""

    observations: list[Observation] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    release_instants: dict[str, dict[date, datetime]] = field(default_factory=dict)
    release_dates: dict[str, dict[date, date]] = field(default_factory=dict)
    cross_check_differences: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _catalog_row(
    definition: SeriesDefinition, mapping: VendorMapping, response: VendorSeriesResponse
) -> dict[str, Any]:
    """Merge the economic catalog with the provenance of this run's delivery."""
    row = definition.metadata_fields()
    row.update(
        {
            "delivery_provider": response.provider,
            "vendor_series_id": response.identifier,
            "vendor_field": response.field,
            "vendor_description": mapping.vendor_description or None,
            "history_start": mapping.history_start,
            "last_publish_date": None,
        }
    )
    return row


def _history_window() -> tuple[date, date]:
    """Return the request window: configured start through today."""
    return date.fromisoformat(HISTORY_START), datetime.now(UTC).date()


def fetch_provider(
    provider: str, mappings: list[VendorMapping], start: date, end: date
) -> dict[str, VendorSeriesResponse]:  # pragma: no cover - requires a live vendor session
    """Retrieve every resolved series from one provider, in one session."""
    if provider == BLOOMBERG:
        from scripts import extract_bloomberg

        session = extract_bloomberg.open_session()
        try:
            return {
                mapping.series_id: extract_bloomberg.fetch_series(session, mapping, start, end)
                for mapping in mappings
            }
        finally:
            session.stop()
    if provider == LSEG:
        from scripts import extract_lseg

        ld = extract_lseg.open_session()
        try:
            return {
                mapping.series_id: extract_lseg.fetch_series(ld, mapping, start, end)
                for mapping in mappings
            }
        finally:
            ld.close_session()
    raise ValueError(f"Unknown delivery provider {provider!r}; expected one of {PROVIDERS}")


def assemble(
    responses: dict[str, VendorSeriesResponse],
    registry: dict[tuple[str, str], VendorMapping],
    provider: str,
) -> CollectedData:
    """Validate provider responses and build everything the run will persist.

    Split out from the session handling so the whole normalisation, validation
    and snapshot path is testable offline against synthetic responses, without a
    Terminal, a Workspace, or any licensed value in this repository.
    """
    data = CollectedData()
    for series_id in sorted(responses):
        response = responses[series_id]
        mapping = registry[(series_id, provider)]
        snapshot = build_snapshot(response)
        data.snapshots.append(snapshot)
        data.observations.extend(canonical_observations(response, snapshot.snapshot_id))
        data.catalog[series_id] = _catalog_row(SERIES_BY_ID[series_id], mapping, response)
        instants = release_instants(response)
        if instants:
            data.release_instants[series_id] = instants
    return data


def collect() -> CollectedData:  # pragma: no cover - requires a live vendor session
    """Run one full collection against the configured providers."""
    registry = load_registry()
    start, end = _history_window()
    primary = DATA_PROVIDER
    mappings = resolved_for(primary, registry)
    pending = pending_for(primary, registry)
    for mapping in pending:
        logger.warning(
            "%s has no confirmed %s identifier; it is skipped, not guessed",
            mapping.series_id,
            primary,
        )
    if not mappings:
        raise PendingVendorDiscoveryError(primary, "every canonical CBI series")

    try:
        responses = fetch_provider(primary, mappings, start, end)
        provider_used = primary
    except (ProviderUnavailableError, TemporaryProviderError) as error:
        if not FALLBACK_PROVIDER or FALLBACK_PROVIDER == primary:
            raise
        logger.warning(
            "Primary provider %s unreachable (%s); trying %s", primary, error, FALLBACK_PROVIDER
        )
        fallback_mappings = resolved_for(FALLBACK_PROVIDER, registry)
        if not fallback_mappings:
            raise PendingVendorDiscoveryError(
                FALLBACK_PROVIDER, "every canonical CBI series"
            ) from error
        responses = fetch_provider(FALLBACK_PROVIDER, fallback_mappings, start, end)
        provider_used = FALLBACK_PROVIDER

    data = assemble(responses, registry, provider_used)
    data.skipped = [mapping.series_id for mapping in pending]

    if CROSS_CHECK_PROVIDER and CROSS_CHECK_PROVIDER != provider_used:
        data.cross_check_differences = run_cross_check(
            responses, registry, CROSS_CHECK_PROVIDER, start, end
        )
    return data


def run_cross_check(  # pragma: no cover - requires a live vendor session
    responses: dict[str, VendorSeriesResponse],
    registry: dict[tuple[str, str], VendorMapping],
    provider: str,
    start: date,
    end: date,
) -> list[str]:
    """Compare the canonical responses against a second provider.

    A cross-check that cannot run is a logged warning, not a failed run: the
    second provider is corroboration, and losing it must not stop the collection
    the desk actually depends on. A cross-check that *does* run and disagrees is
    a failure, because two licensed routes to the same CBI statistic returning
    different numbers means one of the two identifiers is wrong.
    """
    mappings = [
        registry[(series_id, provider)]
        for series_id in sorted(responses)
        if registry[(series_id, provider)].resolved
    ]
    if not mappings:
        logger.warning("Cross-check provider %s has no confirmed identifiers; skipped", provider)
        return []
    try:
        other = fetch_provider(provider, mappings, start, end)
    except VendorError as error:
        logger.warning("Cross-check against %s could not run: %s", provider, error)
        return []
    differences: list[str] = []
    for series_id in sorted(set(responses) & set(other)):
        differences.extend(
            compare_responses(responses[series_id], other[series_id], CROSS_CHECK_TOLERANCE)
        )
    for difference in differences:
        logger.error("Cross-check disagreement: %s", difference)
    return differences
