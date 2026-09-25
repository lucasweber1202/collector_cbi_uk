"""Create the collector_cbi_uk schema and its five standardized tables.

The fleet shape is unchanged: metadata, time_series, availability,
source_snapshots and logs. Two tables carry documented extra columns because
this collector reaches its publisher through a licensed delivery provider
instead of a public file; see the comments above each DDL statement.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from scripts.config import (
    AVAILABILITY_TABLE,
    LOGS_TABLE,
    METADATA_TABLE,
    SCHEMA_NAME,
    SNAPSHOTS_TABLE,
    TIME_SERIES_TABLE,
    VENDOR_PROVENANCE_TABLE,
)
from scripts.db import build_engine
from scripts.legacy_schema import migrate_legacy_metadata

# PostgreSQL and Databricks SQL share no spelling for a 64-bit float. Spark's
# parser lists DOUBLE as the only alias for DoubleType, so Databricks rejects
# DOUBLE PRECISION; PostgreSQL has no DOUBLE and rejects it in turn. FLOAT is
# not a way out: PostgreSQL resolves a bare FLOAT to 8-byte float8 while
# Databricks resolves it to 4-byte FloatType, which would silently halve stored
# precision instead of failing loudly.
DOUBLE_TYPES = {"postgresql": "DOUBLE PRECISION"}
DEFAULT_DOUBLE_TYPE = "DOUBLE"

CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}"

# Legacy metadata columns that must be copied into a sidecar before they
# are dropped, as {column: sidecar table}. These are the vendor fields a
# pre-canonical DDL put inside `metadata`; their home is vendor_provenance.
LEGACY_METADATA_ARCHIVE: dict[str, str] = {
    "seasonal_adjustment": VENDOR_PROVENANCE_TABLE,
    "original_publisher": VENDOR_PROVENANCE_TABLE,
    "delivery_provider": VENDOR_PROVENANCE_TABLE,
    "vendor_series_id": VENDOR_PROVENANCE_TABLE,
    "vendor_field": VENDOR_PROVENANCE_TABLE,
    "vendor_description": VENDOR_PROVENANCE_TABLE,
    "survey": VENDOR_PROVENANCE_TABLE,
    "measure": VENDOR_PROVENANCE_TABLE,
    "category": VENDOR_PROVENANCE_TABLE,
    "reference_date_rule": VENDOR_PROVENANCE_TABLE,
    "release_rule": VENDOR_PROVENANCE_TABLE,
    "revision_policy": VENDOR_PROVENANCE_TABLE,
    "license_context": VENDOR_PROVENANCE_TABLE,
    "history_start": VENDOR_PROVENANCE_TABLE,
    "stance": VENDOR_PROVENANCE_TABLE,
    "sector": VENDOR_PROVENANCE_TABLE,
}

# Two documented deviations from the fleet metadata DDL.
#
# 1. `source_id` carries the source_registry.csv key, as in
#    collector_predictor_template, so a predictor row can be traced to its
#    registry entry without parsing the identifier.
# 2. Vendor provenance columns. This collector does not download a file from the
#    publisher: CBI is the economic publisher, and the data reaches us through a
#    licensed delivery provider (Bloomberg or LSEG). The economic identity of a
#    series and the vendor route used to obtain it are different facts, and
#    collapsing them would make the delivery provider look like the publisher.
#    `original_publisher` therefore always names CBI, while
#    `delivery_provider`, `vendor_series_id` and `vendor_field` record the route
#    that produced the stored history. A series whose vendor identifier has not
#    been confirmed inside the corporate environment carries the explicit
#    sentinel PENDING_VENDOR_DISCOVERY rather than a guess.
CREATE_METADATA_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.{METADATA_TABLE} (
    series_id VARCHAR(200) NOT NULL,
    name VARCHAR(500) NOT NULL,
    description VARCHAR(2000),
    country VARCHAR(3) NOT NULL,
    frequency VARCHAR(20),
    unit VARCHAR(50),
    first_observation DATE,
    last_observation DATE,
    observation_count INTEGER NOT NULL,
    eco_group VARCHAR(250),
    source_url VARCHAR(1000) NOT NULL,
    last_publish_date DATE,
    collected_at TIMESTAMP NOT NULL,
    CONSTRAINT pk_metadata PRIMARY KEY (series_id)
)
"""

# Fleet-standard shape: append-only, one row per stored vintage of an
# observation. Raw published levels only; no derived transformation is stored.
CREATE_TIME_SERIES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.{TIME_SERIES_TABLE} (
    series_id VARCHAR(200) NOT NULL,
    reference_date DATE NOT NULL,
    vintage_date DATE NOT NULL,
    value {{double}} NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    CONSTRAINT pk_time_series PRIMARY KEY (series_id, reference_date, vintage_date)
)
"""

# Point-in-time companion, keyed one-to-one with a time_series row. It answers
# "when did this stored vintage actually become knowable", which vintage_date
# alone cannot: a historical backfill stamps vintage_date with the collection
# day for the whole history. `availability_basis` records how strong the
# evidence for `available_at` is and is never allowed to be blank.
CREATE_AVAILABILITY_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.{AVAILABILITY_TABLE} (
    series_id VARCHAR(200) NOT NULL,
    reference_date DATE NOT NULL,
    vintage_date DATE NOT NULL,
    release_date DATE,
    available_at TIMESTAMP NOT NULL,
    availability_basis VARCHAR(30) NOT NULL,
    source_snapshot_id VARCHAR(64) NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    CONSTRAINT pk_availability PRIMARY KEY (series_id, reference_date, vintage_date)
)
"""

# One row per distinct vendor response actually parsed. A licensed delivery
# provider returns objects over an API rather than a file at a URL, so there are
# no bytes to hash. The snapshot identity is instead the SHA256 of a canonical,
# deterministic serialization of the request and the rows it returned
# (scripts/snapshots.py). Re-running against an unchanged vendor history
# therefore reproduces the same digest and writes nothing, while a vendor that
# restates history produces a new snapshot row beside the old one.
#
# Documented deviation: the vendor provenance columns. `source_url` cannot
# identify a Bloomberg or LSEG response, so the provider, vendor identifier,
# vendor field and the exact query window are stored explicitly. This is what
# lets a stored observation be traced back to "CBI, delivered by Bloomberg,
# under this identifier, over this window, retrieved at this instant".
CREATE_SNAPSHOTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.{SNAPSHOTS_TABLE} (
    snapshot_id VARCHAR(64) NOT NULL,
    source_id VARCHAR(100) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    source_published_date DATE,
    http_etag VARCHAR(200),
    http_last_modified VARCHAR(100),
    sha256 VARCHAR(64) NOT NULL,
    byte_size BIGINT NOT NULL,
    raw_path VARCHAR(1000) NOT NULL,
    delivery_provider VARCHAR(30) NOT NULL,
    vendor_series_id VARCHAR(200) NOT NULL,
    vendor_field VARCHAR(100) NOT NULL,
    vendor_query VARCHAR(2000) NOT NULL,
    vendor_row_count INTEGER NOT NULL,
    CONSTRAINT pk_source_snapshots PRIMARY KEY (snapshot_id)
)
"""

# Vendor delivery provenance, one row per series. Source-specific columns are
# forbidden in the standardized `metadata` table (GUIDELINES.md §3, §11), but the
# licensed delivery path must stay auditable, so they live in this sidecar.
CREATE_VENDOR_PROVENANCE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.{VENDOR_PROVENANCE_TABLE} (
    series_id VARCHAR(200) NOT NULL,
    seasonal_adjustment VARCHAR(30) NOT NULL,
    original_publisher VARCHAR(200) NOT NULL,
    delivery_provider VARCHAR(30) NOT NULL,
    vendor_series_id VARCHAR(200) NOT NULL,
    vendor_field VARCHAR(100) NOT NULL,
    vendor_description VARCHAR(1000),
    survey VARCHAR(100) NOT NULL,
    sector VARCHAR(50) NOT NULL,
    measure VARCHAR(50) NOT NULL,
    category VARCHAR(100) NOT NULL,
    stance VARCHAR(30) NOT NULL,
    -- These four are documentation prose, not codes. `revision_policy` was
    -- declared at 200 while the text this collector writes is longer than
    -- that, so the first INSERT failed on PostgreSQL and would have failed on
    -- Databricks. SQLite ignores VARCHAR lengths entirely, which is why a
    -- SQLite-only test suite never saw it. All four now carry the same
    -- headroom, and tests/test_column_widths.py checks every catalog value
    -- against the width declared here so prose cannot outgrow its column again.
    reference_date_rule VARCHAR(1000) NOT NULL,
    release_rule VARCHAR(1000) NOT NULL,
    revision_policy VARCHAR(1000) NOT NULL,
    license_context VARCHAR(1000) NOT NULL,
    history_start DATE,
    collected_at TIMESTAMP NOT NULL,
    CONSTRAINT pk_vendor_provenance PRIMARY KEY (series_id)
)
"""

CREATE_LOGS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.{LOGS_TABLE} (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP NOT NULL,
    status VARCHAR(20) NOT NULL,
    log_text VARCHAR(65535) NOT NULL,
    traceback VARCHAR(65535),
    CONSTRAINT pk_logs PRIMARY KEY (id)
)
"""


def double_type(dialect: str) -> str:
    """Return the 64-bit float spelling this SQL dialect accepts."""
    return DOUBLE_TYPES.get(dialect, DEFAULT_DOUBLE_TYPE)


def init_db(engine: Engine) -> None:
    """Create all database objects idempotently."""
    double = double_type(engine.dialect.name)
    with engine.begin() as conn:
        for statement in (
            CREATE_SCHEMA,
            CREATE_METADATA_TABLE,
            CREATE_TIME_SERIES_TABLE.format(double=double),
            CREATE_AVAILABILITY_TABLE,
            CREATE_SNAPSHOTS_TABLE,
            CREATE_VENDOR_PROVENANCE_TABLE,
            CREATE_LOGS_TABLE,
        ):
            conn.execute(text(statement))
        # CREATE TABLE IF NOT EXISTS leaves a pre-canonical table untouched,
        # so the live shape is checked against the standardized columns.
        migrate_legacy_metadata(conn, LEGACY_METADATA_ARCHIVE)


if __name__ == "__main__":
    init_db(build_engine())
