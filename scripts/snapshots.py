"""Record which vendor response every stored observation actually came from.

A public-file collector hashes bytes. This one cannot: a licensed delivery
provider answers with objects over an API, and there is no artifact at a URL to
digest. The identity of a snapshot is therefore the SHA-256 of a canonical,
deterministic serialization of the request and the rows it returned
(`normalize.snapshot_payload`), written with sorted keys and no whitespace
variation so the same vendor history always produces the same digest.

What is deliberately *outside* the digest is the retrieval instant. Including it
would give every rerun a new snapshot id, and the run that should have written
nothing would instead write a fresh snapshot row for every series, every day.
The retrieval instant is still recorded, as `fetched_at`, where it is evidence
rather than identity.

The raw file written to disk is the canonical serialization, not a vendor
artifact, and it is gitignored. Licensed CBI values must never enter Git.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scripts.config import PUBLISHER_URL, RAW_DIR, SCHEMA_NAME, SNAPSHOTS_TABLE, SOURCE_ID
from scripts.normalize import VendorSeriesResponse, snapshot_payload

logger = logging.getLogger(__name__)
_TABLE = f"{SCHEMA_NAME}.{SNAPSHOTS_TABLE}"

_COLUMNS = (
    "snapshot_id",
    "source_id",
    "source_url",
    "fetched_at",
    "source_published_date",
    "http_etag",
    "http_last_modified",
    "sha256",
    "byte_size",
    "raw_path",
    "delivery_provider",
    "vendor_series_id",
    "vendor_field",
    "vendor_query",
    "vendor_row_count",
)

_EXISTS_SQL = text(f"SELECT snapshot_id FROM {_TABLE} WHERE snapshot_id = :snapshot_id")
_INSERT_SQL = text(
    f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(f':{column}' for column in _COLUMNS)})"
)


@dataclass(frozen=True)
class Snapshot:
    """One vendor response, identified by the digest of its canonical form."""

    snapshot_id: str
    source_id: str
    source_url: str
    fetched_at: datetime
    source_published_date: date | None
    http_etag: str | None
    http_last_modified: str | None
    sha256: str
    byte_size: int
    raw_path: str
    delivery_provider: str
    vendor_series_id: str
    vendor_field: str
    vendor_query: str
    vendor_row_count: int

    def as_row(self) -> dict[str, Any]:
        """Return the database row for this snapshot."""
        return {column: getattr(self, column) for column in _COLUMNS}


def canonical_bytes(response: VendorSeriesResponse) -> bytes:
    """Serialize a vendor response deterministically."""
    return json.dumps(
        snapshot_payload(response), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(response: VendorSeriesResponse) -> str:
    """Return the SHA-256 identity of a vendor response."""
    return hashlib.sha256(canonical_bytes(response)).hexdigest()


def write_raw_file(response: VendorSeriesResponse, body: bytes, snapshot_id: str) -> Path:
    """Persist the canonical serialization under a gitignored local directory."""
    directory = RAW_DIR / response.provider
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot_id[:16]}-{response.series_id}.json"
    if not path.exists():
        path.write_bytes(body)
    return path


def build_snapshot(
    response: VendorSeriesResponse, source_published_date: date | None = None
) -> Snapshot:
    """Describe one vendor response as an immutable snapshot record."""
    body = canonical_bytes(response)
    snapshot_id = hashlib.sha256(body).hexdigest()
    path = write_raw_file(response, body, snapshot_id)
    return Snapshot(
        snapshot_id=snapshot_id,
        source_id=SOURCE_ID,
        source_url=PUBLISHER_URL,
        fetched_at=response.retrieved_at,
        source_published_date=source_published_date,
        http_etag=None,
        http_last_modified=None,
        sha256=snapshot_id,
        byte_size=len(body),
        raw_path=str(path),
        delivery_provider=response.provider,
        vendor_series_id=response.identifier,
        vendor_field=response.field,
        vendor_query=json.dumps(response.query, sort_keys=True, separators=(",", ":")),
        vendor_row_count=len(response.rows),
    )


def upsert_snapshots(conn: Connection, snapshots: list[Snapshot]) -> int:
    """Insert snapshots not already recorded and return how many were new.

    A snapshot row is immutable: its identity is the content digest, so a row
    that is already present describes exactly this response and is never
    rewritten. This is what makes a rerun against an unchanged vendor history a
    no-op here too.
    """
    inserted = 0
    for snapshot in snapshots:
        if conn.execute(_EXISTS_SQL, {"snapshot_id": snapshot.snapshot_id}).first() is not None:
            logger.info("Snapshot %s already recorded; no rewrite", snapshot.snapshot_id[:12])
            continue
        conn.execute(_INSERT_SQL, snapshot.as_row())
        inserted += 1
        logger.info(
            "Recorded snapshot %s: %s %s (%d rows)",
            snapshot.snapshot_id[:12],
            snapshot.delivery_provider,
            snapshot.vendor_series_id,
            snapshot.vendor_row_count,
        )
    return inserted
