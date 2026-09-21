"""The canonical contract every delivery provider must converge on.

Bloomberg and LSEG return different objects for the same CBI statistic. This
module defines the one shape they are both normalised into, so the rest of the
collector never learns which provider it is reading, and so that
`CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED` means exactly the same stored
series whichever route delivered it. The database never gains a second economic
series because the vendor changed.

Two normalisations matter enough to be done here rather than in either adapter:

* Reference period. Providers stamp a survey period with its first day, its last
  day, or a datetime inside it. For a monthly balance all three mean the same
  survey month and collapse to the first of that month. For a quarterly balance
  they collapse to the first day of the survey *quarter*, so a provider using
  quarter-end stamps and one using quarter-start stamps agree. The catalog, not
  the response, decides which rule applies: a series' frequency is an economic
  fact about the CBI survey, and inferring it from whatever the vendor returned
  would let a wrong identifier define its own period convention. Anything that is
  not a period boundary is rejected rather than truncated, because a mid-period
  stamp means the requested identifier is not the CBI survey balance.
* Publication instants. CBI publishes in London, which is GMT for part of the
  year and BST for the rest. A naive vendor timestamp is therefore interpreted
  in Europe/London through the tz database and converted to UTC, never through
  a fixed offset, which would be wrong for half the year.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from scripts.config import PUBLICATION_TIMEZONE
from scripts.series_catalog import SERIES_BY_ID
from scripts.time_series import Observation
from scripts.vendor_errors import EmptyHistoryError

LONDON = ZoneInfo(PUBLICATION_TIMEZONE)


@dataclass(frozen=True)
class VendorRow:
    """One observation exactly as a delivery provider reported it."""

    reference_date: date
    value: float | None
    release_timestamp: datetime | None = None


@dataclass(frozen=True)
class VendorSeriesResponse:
    """One provider's answer for one canonical series.

    `query` is the full, ordered description of what was asked. It is part of
    the snapshot digest, so a history retrieved over a different window or field
    can never be mistaken for the same artifact.
    """

    provider: str
    series_id: str
    identifier: str
    field: str
    query: dict[str, str]
    rows: tuple[VendorRow, ...]
    retrieved_at: datetime
    vendor_description: str = ""
    warnings: tuple[str, ...] = ()


def to_utc(moment: datetime) -> datetime:
    """Return `moment` in UTC, reading a naive instant as Europe/London.

    GMT and BST are both resolved from the tz database by the date itself. A
    fixed offset would silently shift every summer publication by an hour.
    """
    localised = moment.replace(tzinfo=LONDON) if moment.tzinfo is None else moment
    return localised.astimezone(UTC)


def london_midnight_utc(day: date) -> datetime:
    """Return 00:01 Europe/London on `day`, expressed in UTC.

    CBI survey releases carry an embargo time rather than a publication time in
    every case. This is used only where a release *date* is known without a
    time, and never to invent a release that was not reported. 00:01 is the
    earliest defensible instant on a known release day.
    """
    return to_utc(datetime.combine(day, time(hour=0, minute=1), tzinfo=LONDON))


def _as_date(value: date | datetime | str) -> date:
    """Coerce a provider stamp to a plain date."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        raise TypeError(f"Cannot read {value!r} as a reference date")
    return value


def normalise_reference_date(value: date | datetime | str, frequency: str = "monthly") -> date:
    """Collapse a provider stamp onto the first day of its survey period."""
    stamp = _as_date(value)
    if frequency == "quarterly":
        return date(stamp.year, ((stamp.month - 1) // 3) * 3 + 1, 1)
    return stamp.replace(day=1)


def _is_month_end(value: date) -> bool:
    """True when a date is the last day of its month."""
    next_month = date(value.year + value.month // 12, value.month % 12 + 1, 1)
    return value.day == (next_month - date.resolution).day


def is_period_stamp(value: date, frequency: str) -> bool:
    """True when a stamp is a plausible boundary of its survey period.

    A monthly balance may be stamped at either end of its month. A quarterly
    balance may be stamped at either end of its quarter, but *not* at the
    boundary of an interior month: 2024-02-29 is a month end, and accepting it
    for a quarterly series would quietly relabel a mid-quarter value as Q1.
    """
    if frequency == "quarterly":
        if value.day == 1:
            return value.month in {1, 4, 7, 10}
        return _is_month_end(value) and value.month in {3, 6, 9, 12}
    return value.day == 1 or _is_month_end(value)


def canonical_observations(response: VendorSeriesResponse, snapshot_id: str) -> list[Observation]:
    """Validate one provider response and turn it into canonical observations.

    Every rejection here is a loud failure rather than a dropped row. A CBI
    balance that arrives with mid-period stamps, contradictory duplicates or no
    rows at all is evidence that the identifier or the field is wrong, and
    quietly keeping the rows that happen to parse would store a plausible,
    wrong history.
    """
    if response.series_id not in SERIES_BY_ID:
        raise ValueError(f"{response.series_id} is not a canonical CBI series")
    frequency = SERIES_BY_ID[response.series_id].frequency
    usable = [row for row in response.rows if row.value is not None and math.isfinite(row.value)]
    if not usable:
        raise EmptyHistoryError(
            response.provider,
            f"{response.series_id} ({response.identifier}/{response.field}) returned "
            f"{len(response.rows)} rows and no usable value. An empty history is never "
            "treated as a successful collection.",
        )
    observations: list[Observation] = []
    seen: dict[date, float] = {}
    for row in usable:
        if not is_period_stamp(row.reference_date, frequency):
            raise ValueError(
                f"{response.series_id}: {response.provider} returned {row.reference_date} for a "
                f"{frequency} series. A stamp that is not a {frequency} period boundary means "
                "the requested identifier is not the CBI survey balance."
            )
        reference_date = normalise_reference_date(row.reference_date, frequency)
        value = float(row.value)  # type: ignore[arg-type]
        if reference_date in seen:
            if seen[reference_date] != value:
                raise ValueError(
                    f"{response.series_id}: {response.provider} returned two different values "
                    f"for {reference_date} ({seen[reference_date]} and {value})."
                )
            continue
        seen[reference_date] = value
        observations.append(
            Observation(
                series_id=response.series_id,
                reference_date=reference_date,
                value=value,
                snapshot_id=snapshot_id,
            )
        )
    observations.sort(key=lambda observation: observation.reference_date)
    return observations


def release_instants(response: VendorSeriesResponse) -> dict[date, datetime]:
    """Return the provider-supplied publication instant per reference month.

    Only genuinely reported timestamps appear. A month with no reported instant
    is absent from the mapping, and the caller stamps it `first_seen` rather
    than reconstructing a release time it was never told.
    """
    frequency = SERIES_BY_ID[response.series_id].frequency
    instants: dict[date, datetime] = {}
    for row in response.rows:
        if row.release_timestamp is None:
            continue
        period = normalise_reference_date(row.reference_date, frequency)
        instants[period] = to_utc(row.release_timestamp)
    return instants


def compare_responses(
    primary: VendorSeriesResponse, other: VendorSeriesResponse, tolerance: float
) -> list[str]:
    """Report where a cross-check provider disagrees with the primary.

    Returns human-readable differences rather than raising: a cross-check is
    evidence for the run log and for an operator, not a reason to discard the
    primary's value. The canonical history has exactly one source of truth per
    run, and silently preferring whichever provider happened to answer would
    make the stored series depend on provider availability.
    """
    if primary.series_id != other.series_id:
        raise ValueError("Cross-check compares one canonical series at a time")
    frequency = SERIES_BY_ID[primary.series_id].frequency
    left = {normalise_reference_date(r.reference_date, frequency): r.value for r in primary.rows}
    right = {normalise_reference_date(r.reference_date, frequency): r.value for r in other.rows}
    differences: list[str] = []
    for reference_date in sorted(set(left) & set(right)):
        a, b = left[reference_date], right[reference_date]
        if a is None or b is None:
            continue
        if abs(a - b) > tolerance:
            differences.append(
                f"{primary.series_id} {reference_date}: {primary.provider}={a} "
                f"{other.provider}={b} (tolerance {tolerance})"
            )
    only_primary = sorted(set(left) - set(right))
    only_other = sorted(set(right) - set(left))
    if only_primary:
        differences.append(
            f"{primary.series_id}: {len(only_primary)} months only in {primary.provider} "
            f"(first {only_primary[0]}, last {only_primary[-1]})"
        )
    if only_other:
        differences.append(
            f"{primary.series_id}: {len(only_other)} months only in {other.provider} "
            f"(first {only_other[0]}, last {only_other[-1]})"
        )
    return differences


def snapshot_payload(response: VendorSeriesResponse) -> dict[str, Any]:
    """Return the deterministic description of a response for hashing.

    `retrieved_at` is deliberately excluded. It changes on every run, and
    including it would make every rerun produce a new snapshot digest, which
    would destroy the idempotency the snapshot table exists to provide. The
    retrieval instant is still recorded, in the snapshot row's `fetched_at`
    column, where it is evidence rather than identity.
    """
    return {
        "provider": response.provider,
        "series_id": response.series_id,
        "identifier": response.identifier,
        "field": response.field,
        "query": {key: response.query[key] for key in sorted(response.query)},
        "rows": [
            {
                "reference_date": row.reference_date.isoformat(),
                "value": row.value,
                "release_timestamp": (
                    to_utc(row.release_timestamp).isoformat()
                    if row.release_timestamp is not None
                    else None
                ),
            }
            for row in sorted(response.rows, key=lambda row: row.reference_date)
        ],
    }
