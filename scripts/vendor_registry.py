"""The versioned map from canonical series to licensed-provider identifiers.

This file is the only place a Bloomberg ticker or an LSEG RIC is allowed to
appear, and none is present until someone confirms it inside an entitled
session. Every unconfirmed cell holds the literal sentinel
PENDING_VENDOR_DISCOVERY. That is not a placeholder to be filled in with a
plausible guess: an invented ticker either fails loudly or, far worse, resolves
to a different statistic and silently poisons the history.

Filling the registry in is the whole deployment step. Nothing in the parser, the
normaliser or the database has to change when a real identifier arrives.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from scripts.config import PROVIDERS, VENDOR_SERIES_FILE
from scripts.series_catalog import SERIES_BY_ID

PENDING = "PENDING_VENDOR_DISCOVERY"
FIELDNAMES = (
    "series_id",
    "provider",
    "identifier",
    "field",
    "vendor_description",
    "history_start",
    "confirmed_on",
    "notes",
)


@dataclass(frozen=True)
class VendorMapping:
    """How one canonical series is requested from one delivery provider."""

    series_id: str
    provider: str
    identifier: str
    field: str
    vendor_description: str
    history_start: date | None
    confirmed_on: date | None
    notes: str

    @property
    def resolved(self) -> bool:
        """True only when both the instrument and the field are confirmed."""
        return self.identifier != PENDING and self.field != PENDING


def _as_optional_date(value: str, column: str, series_id: str) -> date | None:
    """Parse an ISO date cell, treating blank and the sentinel as unknown."""
    text = value.strip()
    if not text or text == PENDING:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{series_id}: {column} must be an ISO date, got {text!r}") from error


def load_registry(path: Path = VENDOR_SERIES_FILE) -> dict[tuple[str, str], VendorMapping]:
    """Load and validate the registry, keyed by (series_id, provider).

    Validation is strict on purpose. A typo in a series id or a provider name
    would otherwise present as "this series has no mapping", which is
    indistinguishable from an honest pending entry and would hide a real
    identifier that had already been discovered.
    """
    if not path.exists():
        raise FileNotFoundError(f"Vendor series registry not found at {path}")
    mappings: dict[tuple[str, str], VendorMapping] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or tuple(reader.fieldnames) != FIELDNAMES:
            raise ValueError(
                f"{path} must have exactly the columns {FIELDNAMES}, got {reader.fieldnames}"
            )
        for row in reader:
            series_id = (row["series_id"] or "").strip()
            provider = (row["provider"] or "").strip().lower()
            if series_id not in SERIES_BY_ID:
                raise ValueError(f"{path}: {series_id!r} is not a canonical CBI series")
            if provider not in PROVIDERS:
                raise ValueError(f"{path}: {series_id} has unknown provider {provider!r}")
            key = (series_id, provider)
            if key in mappings:
                raise ValueError(f"{path}: duplicate row for {series_id} / {provider}")
            identifier = (row["identifier"] or "").strip()
            field = (row["field"] or "").strip()
            if not identifier or not field:
                raise ValueError(
                    f"{path}: {series_id} / {provider} must state an identifier and a field, "
                    f"using {PENDING} when it has not been confirmed"
                )
            confirmed_on = _as_optional_date(row["confirmed_on"], "confirmed_on", series_id)
            if identifier != PENDING and confirmed_on is None:
                raise ValueError(
                    f"{path}: {series_id} / {provider} states identifier {identifier!r} but no "
                    "confirmed_on date. An identifier is recorded only once it has been "
                    "verified against the live provider."
                )
            mappings[key] = VendorMapping(
                series_id=series_id,
                provider=provider,
                identifier=identifier,
                field=field,
                vendor_description=(row["vendor_description"] or "").strip(),
                history_start=_as_optional_date(row["history_start"], "history_start", series_id),
                confirmed_on=confirmed_on,
                notes=(row["notes"] or "").strip(),
            )
    missing = [
        (series_id, provider)
        for series_id in SERIES_BY_ID
        for provider in PROVIDERS
        if (series_id, provider) not in mappings
    ]
    if missing:
        raise ValueError(
            f"{path} is missing rows for {missing}. Every canonical series must have a row "
            f"for every provider, even if it is {PENDING}."
        )
    return mappings


def resolved_for(
    provider: str, registry: dict[tuple[str, str], VendorMapping]
) -> list[VendorMapping]:
    """Return this provider's confirmed mappings, in canonical id order."""
    return [
        registry[(series_id, provider)]
        for series_id in sorted(SERIES_BY_ID)
        if registry[(series_id, provider)].resolved
    ]


def pending_for(
    provider: str, registry: dict[tuple[str, str], VendorMapping]
) -> list[VendorMapping]:
    """Return this provider's unconfirmed mappings, in canonical id order."""
    return [
        registry[(series_id, provider)]
        for series_id in sorted(SERIES_BY_ID)
        if not registry[(series_id, provider)].resolved
    ]
