"""Read CBI survey history from Bloomberg through the official blpapi library.

Bloomberg is a licensed *delivery provider* here, never the publisher. The
statistic stays a Confederation of British Industry publication; Bloomberg is
the route the desk is entitled to use.

Why blpapi and not a wrapper: blpapi is Bloomberg's own Python API and is the
only client the Terminal's Desktop API service is documented against. A wrapper
such as xbbg is a convenience layer over the same library, adds a pandas
dependency this collector does not otherwise need, and hides exactly the
response-level detail — securityError, fieldException, the responseError
element — that this module has to inspect to tell an unentitled request apart
from a genuinely empty one. There is no requirement blpapi cannot meet here, so
there is no second dependency.

The import is deliberately local to the functions that need it. blpapi is an
optional extra (`pip install '.[bloomberg]'`) and is absent on every machine
without a Terminal, so importing it at module scope would make the offline test
suite unrunnable on any developer laptop.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from scripts.config import BLOOMBERG, BLOOMBERG_HOST, BLOOMBERG_PORT
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

REFERENCE_DATA_SERVICE = "//blp/refdata"
INSTRUMENTS_SERVICE = "//blp/instruments"
FIELDS_SERVICE = "//blp/apiflds"

# Bloomberg reports "you may not see this" and "this does not exist" through the
# same securityError element, separated only by the subcategory. Conflating them
# would send an operator hunting for a bad ticker when the real answer is that
# the desk needs an entitlement, so the two are mapped to different exceptions.
_ENTITLEMENT_SUBCATEGORIES = frozenset(
    {"NOT_ENTITLED_TO_SECURITY", "NOT_ENTITLED_TO_FIELD", "NOT_AUTHORIZED"}
)
_AUTHENTICATION_SUBCATEGORIES = frozenset(
    {"NOT_LOGGED_IN", "INVALID_USER", "AUTHORIZATION_FAILURE"}
)


def _blpapi() -> Any:
    """Import blpapi, or explain precisely why this machine cannot use it."""
    try:
        import blpapi  # type: ignore[import-not-found]
    except ImportError as error:  # pragma: no cover - environment dependent
        raise ProviderUnavailableError(
            BLOOMBERG,
            "blpapi is not installed. Install the Bloomberg extra on an entitled "
            "machine with: pip install '.[bloomberg]'",
        ) from error
    return blpapi


def open_session() -> Any:  # pragma: no cover - requires a live Terminal
    """Start a Desktop API session against the locally running Terminal."""
    blpapi = _blpapi()
    options = blpapi.SessionOptions()
    options.setServerHost(BLOOMBERG_HOST)
    options.setServerPort(BLOOMBERG_PORT)
    session = blpapi.Session(options)
    if not session.start():
        raise ProviderUnavailableError(
            BLOOMBERG,
            f"Could not start a Bloomberg session on {BLOOMBERG_HOST}:{BLOOMBERG_PORT}. The "
            "Terminal must be running and logged in on this machine for the Desktop API to "
            "answer.",
        )
    return session


def open_service(session: Any, service: str) -> Any:  # pragma: no cover - live session
    """Open one Bloomberg service, distinguishing absence from unavailability."""
    if not session.openService(service):
        raise EntitlementError(
            BLOOMBERG,
            f"Could not open {service}. The session is running but this user is not "
            "entitled to that service.",
        )
    return session.getService(service)


def _element(message: Any, name: str) -> Any | None:
    """Return a named element when the message actually carries it."""
    return message.getElement(name) if message.hasElement(name) else None


def inspect_security_error(error: Any, identifier: str, field: str) -> Exception:
    """Translate a Bloomberg error element into the right explicit exception."""
    subcategory = (
        str(error.getElementAsString("subcategory")) if error.hasElement("subcategory") else ""
    )
    message = (
        str(error.getElementAsString("message")) if error.hasElement("message") else str(error)
    )
    if subcategory in _ENTITLEMENT_SUBCATEGORIES:
        return EntitlementError(BLOOMBERG, f"Not entitled to {identifier} / {field}: {message}")
    if subcategory in _AUTHENTICATION_SUBCATEGORIES:
        return VendorAuthenticationError(BLOOMBERG, f"Session not authorised: {message}")
    if subcategory == "INVALID_SECURITY" or "INVALID_SECURITY" in message.upper():
        return IdentifierNotFoundError(
            BLOOMBERG,
            f"Bloomberg does not recognise {identifier!r}: {message}. Re-run "
            "scripts.discover_series rather than adjusting the ticker by hand.",
        )
    if "FIELD" in subcategory.upper() or "FIELD" in message.upper():
        return FieldNotFoundError(BLOOMBERG, f"Field {field!r} invalid for {identifier}: {message}")
    return TemporaryProviderError(
        BLOOMBERG, f"Unclassified error for {identifier} / {field}: {subcategory} {message}"
    )


def parse_historical_response(
    messages: list[Any], mapping: VendorMapping, query: dict[str, str], retrieved_at: datetime
) -> VendorSeriesResponse:
    """Turn HistoricalDataResponse messages into the canonical vendor response.

    Kept free of any live session so it is unit-testable against recorded
    response *shapes* — never against real CBI values, which are licensed and
    must not enter this repository.
    """
    rows: list[VendorRow] = []
    warnings: list[str] = []
    for message in messages:
        response_error = _element(message, "responseError")
        if response_error is not None:
            raise inspect_security_error(response_error, mapping.identifier, mapping.field)
        security_data = _element(message, "securityData")
        if security_data is None:
            continue
        security_error = _element(security_data, "securityError")
        if security_error is not None:
            raise inspect_security_error(security_error, mapping.identifier, mapping.field)
        exceptions = _element(security_data, "fieldExceptions")
        if exceptions is not None:
            for index in range(exceptions.numValues()):
                info = exceptions.getValueAsElement(index)
                error = _element(info, "errorInfo")
                if error is not None:
                    raise inspect_security_error(error, mapping.identifier, mapping.field)
        field_data = _element(security_data, "fieldData")
        if field_data is None:
            continue
        for index in range(field_data.numValues()):
            point = field_data.getValueAsElement(index)
            if not point.hasElement("date") or not point.hasElement(mapping.field):
                warnings.append(f"Skipped a point without date or {mapping.field}")
                continue
            rows.append(
                VendorRow(
                    reference_date=_as_date(point.getElementAsDatetime("date")),
                    value=float(point.getElementAsFloat(mapping.field)),
                )
            )
    return VendorSeriesResponse(
        provider=BLOOMBERG,
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
    """Coerce a blpapi datetime-like value to a plain date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date(value.year, value.month, value.day)


def build_query(mapping: VendorMapping, start: date, end: date) -> dict[str, str]:
    """Describe the request in the exact form that identifies the artifact."""
    return {
        "service": REFERENCE_DATA_SERVICE,
        "request": "HistoricalDataRequest",
        "security": mapping.identifier,
        "field": mapping.field,
        "periodicitySelection": "MONTHLY",
        "periodicityAdjustment": "CALENDAR",
        "nonTradingDayFillOption": "ALL_CALENDAR_DAYS",
        "nonTradingDayFillMethod": "NIL_VALUE",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
    }


def fetch_series(  # pragma: no cover - requires a live Terminal
    session: Any, mapping: VendorMapping, start: date, end: date
) -> VendorSeriesResponse:
    """Retrieve one canonical series' full history from Bloomberg."""
    blpapi = _blpapi()
    service = open_service(session, REFERENCE_DATA_SERVICE)
    query = build_query(mapping, start, end)
    request = service.createRequest("HistoricalDataRequest")
    request.getElement("securities").appendValue(mapping.identifier)
    request.getElement("fields").appendValue(mapping.field)
    for key in (
        "periodicitySelection",
        "periodicityAdjustment",
        "nonTradingDayFillOption",
        "nonTradingDayFillMethod",
    ):
        request.set(key, query[key])
    request.set("startDate", start.strftime("%Y%m%d"))
    request.set("endDate", end.strftime("%Y%m%d"))
    logger.info(
        "Bloomberg HistoricalDataRequest for %s (%s)", mapping.series_id, mapping.identifier
    )
    session.sendRequest(request)
    messages: list[Any] = []
    while True:
        event = session.nextEvent(120_000)
        event_type = event.eventType()
        if event_type == blpapi.Event.TIMEOUT:
            raise TemporaryProviderError(
                BLOOMBERG, f"Timed out waiting for {mapping.identifier}; retry this run."
            )
        messages.extend(list(event))
        if event_type == blpapi.Event.RESPONSE:
            break
    return parse_historical_response(messages, mapping, query, datetime.now(UTC))
