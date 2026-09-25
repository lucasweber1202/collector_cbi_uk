"""Every stored string must fit the column width the shipped DDL declares.

SQLite ignores VARCHAR lengths completely, so a suite that only ever runs on
SQLite cannot see an oversized value. PostgreSQL and Databricks both reject it,
which means an overflow here is not a cosmetic issue: it fails the collector's
first INSERT on the engines production actually uses.

This is how `revision_policy` shipped as VARCHAR(200) while the prose written
into it was over 200 characters. The check reads the widths out of the DDL
rather than restating them, so widening a column updates the test with it and
lengthening the prose past the column still fails.
"""

from __future__ import annotations

import re

import pytest

from scripts import init_db
from scripts.series_catalog import ALL_SERIES

_DDL = {
    "metadata": init_db.CREATE_METADATA_TABLE,
    "vendor_provenance": init_db.CREATE_VENDOR_PROVENANCE_TABLE,
}


def _declared_widths(ddl: str) -> dict[str, int]:
    """Read ``column -> max length`` out of a CREATE TABLE statement."""
    return {
        name: int(width)
        for name, width in re.findall(r"^\s*(\w+) VARCHAR\((\d+)\)", ddl, re.MULTILINE)
    }


def _catalog_values() -> dict[str, int]:
    """Return the longest string this collector would store, per column."""
    longest: dict[str, int] = {}
    for series in ALL_SERIES:
        raw = series.metadata_fields
        fields: dict[str, object] = raw() if callable(raw) else raw
        for column, value in fields.items():
            if isinstance(value, str):
                longest[column] = max(longest.get(column, 0), len(value))
        longest["series_id"] = max(longest.get("series_id", 0), len(series.series_id))
    return longest


ALL_WIDTHS = {
    column: width for ddl in _DDL.values() for column, width in _declared_widths(ddl).items()
}
CHECKED = sorted(column for column in _catalog_values() if column in ALL_WIDTHS)


def test_the_ddl_declares_widths_at_all() -> None:
    """Guard the guard: a DDL rewrite must not make this test vacuous."""
    assert ALL_WIDTHS, "no VARCHAR widths parsed out of the shipped DDL"
    assert CHECKED, "no catalog column matched a declared column"
    assert "revision_policy" in CHECKED, "the column this test was written for is not covered"


@pytest.mark.parametrize("column", CHECKED)
def test_catalog_values_fit_their_column(column: str) -> None:
    longest = _catalog_values()[column]
    declared = ALL_WIDTHS[column]
    assert longest <= declared, (
        f"{column} holds up to {longest} characters but is declared VARCHAR({declared}); "
        "PostgreSQL and Databricks will reject the insert, SQLite will not"
    )
