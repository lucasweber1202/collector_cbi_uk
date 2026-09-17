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
    PROVIDERS,
)
from scripts.normalize import (
    VendorSeriesResponse,
    canonical_observations,
    compare_responses,
    release_instants,
)
from scripts.series_catalog import SERIES_BY_ID, SeriesDefinition
from scripts.snapshots import Snapshot, build_snapshot
from scripts.time_series import Observation
from scripts.vendor_errors import (
    PendingVendorDiscoveryError,
    ProviderUnavailableError,
    TemporaryProviderError,
    VendorError,
)
from scripts.vendor_registry import VendorMapping, load_registry, pending_for, resolved_for

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
