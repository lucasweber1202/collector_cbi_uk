"""Find the vendor identifiers for the canonical CBI series, on an entitled machine.

This exists to make the one step that cannot be done outside the corporate
environment fast, repeatable and written down, instead of a person clicking
through a Terminal and pasting a ticker into a config file from memory.

    python -m scripts.discover_series --provider bloomberg --query "CBI selling prices"
    python -m scripts.discover_series --provider lseg --query "CBI selling prices"

Both providers do support programmatic search, so this is a real tool rather
than a placeholder:

* Bloomberg exposes `//blp/instruments`, whose `instrumentListRequest` takes a
  free-text query and returns matching securities with descriptions, and
  `//blp/apiflds`, whose `FieldSearchRequest` returns field mnemonics matching a
  description. Together they answer both halves of a registry row.
* LSEG exposes `lseg.data.discovery.search`, which takes a free-text query and a
  view and returns matching instruments with their RICs and descriptions.

What this tool deliberately does NOT do is write to config/vendor_series.csv. A
search result is a candidate, not a confirmation. Somebody has to look at the
description, check that the history actually starts where CBI's does and that
the field returns the published rate, and only then record the identifier
together with the date they confirmed it. An automatic write would turn "the
search returned something" into "the ticker is correct", which is precisely the
failure this repository refuses to risk.

The output is the exact CSV cells to paste, so the manual step is a review, not
a transcription.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from scripts.config import BLOOMBERG, LSEG, PROVIDERS
from scripts.series_catalog import PRIORITY_PRICE_SERIES
from scripts.vendor_errors import ProviderUnavailableError, VendorError

logger = logging.getLogger(__name__)

# Ordered so the balances this collector exists for come first. CBI's three
# surveys each ask a selling-price question, and a single "CBI selling prices"
# search will return candidates from all three: the survey each candidate
# belongs to has to be read off its description, not assumed from its position.
DEFAULT_QUERIES = (
    "CBI distributive trades selling prices",
    "CBI industrial trends selling prices",
    "CBI service sector prices",
    "CBI selling prices expectations",
    "CBI average costs",
    "CBI distributive trades",
    "CBI industrial trends",
    "CBI service sector survey",
)
MAX_RESULTS = 50


def discover_bloomberg(query: str, limit: int) -> list[dict[str, str]]:  # pragma: no cover
    """Search Bloomberg's instrument catalogue for a free-text query."""
    from scripts import extract_bloomberg

    blpapi = extract_bloomberg._blpapi()
    session = extract_bloomberg.open_session()
    try:
        service = extract_bloomberg.open_service(session, extract_bloomberg.INSTRUMENTS_SERVICE)
        request = service.createRequest("instrumentListRequest")
        request.set("query", query)
        request.set("maxResults", limit)
        session.sendRequest(request)
        results: list[dict[str, str]] = []
        while True:
            event = session.nextEvent(60_000)
            if event.eventType() == blpapi.Event.TIMEOUT:
                raise VendorError(BLOOMBERG, f"Timed out searching for {query!r}")
            for message in event:
                if not message.hasElement("results"):
                    continue
                found = message.getElement("results")
                for index in range(found.numValues()):
                    item = found.getValueAsElement(index)
                    results.append(
                        {
                            "identifier": _element_text(item, "security"),
                            "description": _element_text(item, "description"),
                        }
                    )
            if event.eventType() == blpapi.Event.RESPONSE:
                break
        return results
    finally:
        session.stop()


def discover_bloomberg_fields(query: str, limit: int) -> list[dict[str, str]]:  # pragma: no cover
    """Search Bloomberg's field dictionary so the `field` cell is not guessed either."""
    from scripts import extract_bloomberg

    blpapi = extract_bloomberg._blpapi()
    session = extract_bloomberg.open_session()
    try:
        service = extract_bloomberg.open_service(session, extract_bloomberg.FIELDS_SERVICE)
        request = service.createRequest("FieldSearchRequest")
        request.set("searchSpec", query)
        session.sendRequest(request)
        results: list[dict[str, str]] = []
        while True:
            event = session.nextEvent(60_000)
            if event.eventType() == blpapi.Event.TIMEOUT:
                raise VendorError(BLOOMBERG, f"Timed out searching fields for {query!r}")
            for message in event:
                if not message.hasElement("fieldData"):
                    continue
                found = message.getElement("fieldData")
                for index in range(min(found.numValues(), limit)):
                    item = found.getValueAsElement(index)
                    info = item.getElement("fieldInfo") if item.hasElement("fieldInfo") else item
                    results.append(
                        {
                            "identifier": _element_text(info, "mnemonic"),
                            "description": _element_text(info, "description"),
                        }
                    )
            if event.eventType() == blpapi.Event.RESPONSE:
                break
        return results
    finally:
        session.stop()


def _element_text(element: Any, name: str) -> str:
    """Read one element as text when it is present."""
    return str(element.getElementAsString(name)) if element.hasElement(name) else ""


def discover_lseg(query: str, limit: int) -> list[dict[str, str]]:  # pragma: no cover
    """Search LSEG's discovery index for a free-text query."""
    from scripts import extract_lseg

    ld = extract_lseg.open_session()
    try:
        try:
            frame = ld.discovery.search(query=query, top=limit)
        except Exception as error:
            raise extract_lseg.classify_error(error, query, "") from error
        if frame is None or len(frame) == 0:
            return []
        columns = [str(column) for column in frame.columns]
        identifier_column = next(
            (name for name in ("RIC", "DocumentTitle", "PermID") if name in columns), columns[0]
        )
        description_column = next(
            (
                name
                for name in ("DocumentTitle", "Description", "BusinessEntity")
                if name in columns
            ),
            identifier_column,
        )
        return [
            {
                "identifier": str(row[identifier_column]),
                "description": str(row[description_column]),
            }
            for _, row in frame.iterrows()
        ]
    finally:
        ld.close_session()


def format_results(provider: str, query: str, results: list[dict[str, str]]) -> str:
    """Render search results as reviewable text plus paste-ready registry cells."""
    lines = [f"# {provider}: {len(results)} result(s) for {query!r}"]
    if not results:
        lines.append(
            "#   no match. Try a different wording before concluding the series is absent."
        )
        return "\n".join(lines)
    for result in results:
        lines.append(f"  {result['identifier']:<28} {result['description']}")
    lines.append("")
    lines.append("# Nothing above is confirmed. For each canonical series, verify that the")
    lines.append("# candidate's description names the right CBI SURVEY and the right")
    lines.append("# current/expected horizon, that its history starts where CBI's does, and")
    lines.append("# that the field returns the published balance. A selling-price balance")
    lines.append("# from the wrong survey looks entirely plausible and is entirely wrong.")
    lines.append("# Then edit config/vendor_series.csv, replacing PENDING_VENDOR_DISCOVERY")
    lines.append("# and setting confirmed_on to today. Priority rows for this provider:")
    for series in PRIORITY_PRICE_SERIES:
        lines.append(f"#   {series.series_id},{provider},<identifier>,<field>,...")
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the discovery command line."""
    parser = argparse.ArgumentParser(
        description="Search a licensed delivery provider for CBI series identifiers."
    )
    parser.add_argument("--provider", required=True, choices=list(PROVIDERS))
    parser.add_argument(
        "--query",
        action="append",
        help="Free-text search. Repeatable. Defaults to a set of CBI survey wordings.",
    )
    parser.add_argument(
        "--fields",
        action="store_true",
        help="Bloomberg only: search the field dictionary instead of the instrument catalogue.",
    )
    parser.add_argument("--limit", type=int, default=MAX_RESULTS)
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:  # pragma: no cover - requires a live vendor
    """Search the provider and print reviewable candidates."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    queries = args.query or list(DEFAULT_QUERIES)
    try:
        for query in queries:
            if args.provider == BLOOMBERG:
                results = (
                    discover_bloomberg_fields(query, args.limit)
                    if args.fields
                    else discover_bloomberg(query, args.limit)
                )
            elif args.provider == LSEG:
                results = discover_lseg(query, args.limit)
            else:
                raise ValueError(f"Unknown provider {args.provider!r}")
            print(format_results(args.provider, query, results))
            print()
    except ProviderUnavailableError as error:
        print(f"{error}", file=sys.stderr)
        return 2
    except VendorError as error:
        print(f"{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run(sys.argv[1:]))
