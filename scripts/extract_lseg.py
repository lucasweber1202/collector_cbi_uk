"""Read CBI survey history from LSEG through the official LSEG Data Library.

LSEG is a licensed *delivery provider*, not the publisher. The library is
`lseg-data` (imported as `lseg.data`), the currently recommended official client
and the successor to the Refinitiv Data Library; a Desktop session authenticates
through a running LSEG Workspace on the same machine, so this module never holds
a credential. Where a desk runs a platform session instead, the library reads
its own `lseg-data.config.json` — pointed at by LD_LIB_CONFIG_PATH — and this
module still holds nothing.

As with Bloomberg, the import is local to the functions that need it: the
library is an optional extra (`pip install '.[lseg]'`) and the offline test
suite must run on a machine that has never seen Workspace.

The failure taxonomy matters more here than the happy path. `get_history`
answers an unknown RIC, an unentitled instrument and an empty window in ways
that all reduce to "nothing came back" unless the exception and the returned
object are inspected, and a collector that shrugs at nothing coming back is the
one failure this fleet cannot ship.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from scripts.config import LSEG, LSEG_SESSION
from scripts.normalize import VendorRow, VendorSeriesResponse
from scripts.vendor_errors import (
    EntitlementError,
    FieldNotFoundError,
    IdentifierNotFoundError,
    ProviderUnavailableError,
    TemporaryProviderError,
    VendorAuthenticationError,
)
from scripts.vendor_registry import VendorMapping

logger = logging.getLogger(__name__)

# Matched against the text of whatever the library raises. The library's error
# classes have moved between Refinitiv and LSEG releases, so classifying on
# message content is more durable here than pinning an exception class, and an
# unmatched message deliberately falls through to TemporaryProviderError rather
# than being assumed benign.
_ENTITLEMENT_MARKERS = (
    "not entitled",
    "no permission",
    "insufficient entitlement",
    "access denied",
)
_AUTHENTICATION_MARKERS = ("authentication", "unauthorized", "invalid token", "login failed")
_IDENTIFIER_MARKERS = (
    "invalid ric",
    "instrument not found",
    "no data available for the requested",
    "unknown instrument",
    "invalid universe",
)
_FIELD_MARKERS = ("invalid field", "field not found", "unknown field")
_TEMPORARY_MARKERS = (
    "timeout",
    "timed out",
    "service unavailable",
    "too many requests",
    "connection reset",
    "temporarily",
)


def _lseg_data() -> Any:
    """Import lseg.data, or explain precisely why this machine cannot use it."""
    try:
        import lseg.data as ld  # type: ignore[import-not-found]
    except ImportError as error:  # pragma: no cover - environment dependent
        raise ProviderUnavailableError(
            LSEG,
            "lseg-data is not installed. Install the LSEG extra on an entitled "
            "machine with: pip install '.[lseg]'",
        ) from error
    return ld


def classify_error(error: Exception, identifier: str, field: str) -> Exception:
    """Translate a library exception into the right explicit collector error."""
    text = str(error).lower()
    if any(marker in text for marker in _ENTITLEMENT_MARKERS):
        return EntitlementError(LSEG, f"Not entitled to {identifier} / {field}: {error}")
    if any(marker in text for marker in _AUTHENTICATION_MARKERS):
        return VendorAuthenticationError(
            LSEG, f"Workspace session not authenticated ({LSEG_SESSION}): {error}"
        )
    if any(marker in text for marker in _FIELD_MARKERS):
        return FieldNotFoundError(LSEG, f"Field {field!r} invalid for {identifier}: {error}")
    if any(marker in text for marker in _IDENTIFIER_MARKERS):
        return IdentifierNotFoundError(
            LSEG,
            f"LSEG does not recognise {identifier!r}: {error}. Re-run "
            "scripts.discover_series rather than adjusting the RIC by hand.",
        )
    if any(marker in text for marker in _TEMPORARY_MARKERS):
        return TemporaryProviderError(LSEG, f"Transient failure for {identifier}: {error}")
    return TemporaryProviderError(
        LSEG,
        f"Unclassified LSEG failure for {identifier} / {field}: {error}. Treated as "
        "retryable, but a repeat means the taxonomy in scripts/extract_lseg.py needs a "
        "new case rather than a broader catch.",
    )


def open_session() -> Any:  # pragma: no cover - requires a live Workspace
    """Open the configured LSEG session."""
    ld = _lseg_data()
    try:
        ld.open_session(LSEG_SESSION)
    except Exception as error:
        if "config" in str(error).lower() or "session" in str(error).lower():
            raise ProviderUnavailableError(
                LSEG,
                f"Could not open the {LSEG_SESSION!r} session. LSEG Workspace must be running "
                "and signed in on this machine, or LD_LIB_CONFIG_PATH must point at a "
                f"lseg-data.config.json defining it. Underlying error: {error}",
            ) from error
        raise classify_error(error, LSEG_SESSION, "") from error
    return ld


def build_query(mapping: VendorMapping, start: date, end: date) -> dict[str, str]:
    """Describe the request in the exact form that identifies the artifact."""
    return {
        "library": "lseg.data",
        "call": "get_history",
        "session": LSEG_SESSION,
        "universe": mapping.identifier,
        "fields": mapping.field,
        "interval": "monthly",
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def parse_history_records(
    records: list[dict[str, Any]],
    mapping: VendorMapping,
    query: dict[str, str],
    retrieved_at: datetime,
) -> VendorSeriesResponse:
    """Turn plain record dictionaries into the canonical vendor response.

    `records` is the library's result already reduced to `{date, value}` (and
    optionally a reported release instant), which keeps this function free of
    pandas and testable without the library installed.
    """
    rows: list[VendorRow] = []
    warnings: list[str] = []
    for record in records:
        raw_date = record.get("date")
        if raw_date is None:
            warnings.append("Skipped a record without a date")
            continue
        value = record.get(mapping.field, record.get("value"))
        release = record.get("release_timestamp")
        rows.append(
            VendorRow(
                reference_date=_as_date(raw_date),
                value=None if value is None else float(value),
                release_timestamp=_as_datetime(release) if release is not None else None,
            )
        )
    return VendorSeriesResponse(
        provider=LSEG,
        series_id=mapping.series_id,
        identifier=mapping.identifier,
        field=mapping.field,
        query=query,
        rows=tuple(rows),
        retrieved_at=retrieved_at,
        vendor_description=mapping.vendor_description,
        warnings=tuple(warnings),
    )


def _as_date(value: Any) -> date:
    """Coerce a library-returned index value to a plain date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    if hasattr(value, "to_pydatetime"):
        return _as_date(value.to_pydatetime())
    raise TypeError(f"Cannot read {value!r} as a date")


def _as_datetime(value: Any) -> datetime:
    """Coerce a library-returned timestamp to a datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if hasattr(value, "to_pydatetime"):
        return _as_datetime(value.to_pydatetime())
    raise TypeError(f"Cannot read {value!r} as a timestamp")


def dataframe_to_records(frame: Any, field: str) -> list[dict[str, Any]]:
    """Reduce the library's DataFrame to plain records.

    The column the library returns is not guaranteed to be spelled exactly like
    the requested field, but a frame carrying several columns is ambiguous and
    is rejected rather than guessed at.
    """
    columns = [str(column) for column in frame.columns]
    if field in columns:
        column = field
    elif len(columns) == 1:
        column = columns[0]
    else:
        raise FieldNotFoundError(
            LSEG,
            f"Requested field {field!r} is not in the returned columns {columns}; refusing "
            "to guess which column is the CBI value.",
        )
    return [
        {"date": index, field: None if _is_missing(value) else float(value)}
        for index, value in zip(frame.index, frame[column], strict=True)
    ]


def _is_missing(value: Any) -> bool:
    """True for None and for a float NaN, without importing pandas."""
    return value is None or (isinstance(value, float) and value != value)


def fetch_series(  # pragma: no cover - requires a live Workspace
    ld: Any, mapping: VendorMapping, start: date, end: date
) -> VendorSeriesResponse:
    """Retrieve one canonical series' full history from LSEG."""
    query = build_query(mapping, start, end)
    logger.info("LSEG get_history for %s (%s)", mapping.series_id, mapping.identifier)
    try:
        frame = ld.get_history(
            universe=mapping.identifier,
            fields=[mapping.field],
            interval="monthly",
            start=start.isoformat(),
            end=end.isoformat(),
        )
    except Exception as error:
        raise classify_error(error, mapping.identifier, mapping.field) from error
    if frame is None:
        raise IdentifierNotFoundError(
            LSEG,
            f"get_history returned nothing at all for {mapping.identifier!r}. This is not an "
            "empty history; the library returns None when the universe did not resolve.",
        )
    return parse_history_records(
        dataframe_to_records(frame, mapping.field), mapping, query, datetime.now(UTC)
    )
