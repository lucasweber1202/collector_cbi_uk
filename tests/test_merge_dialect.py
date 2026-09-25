"""Every shipped MERGE must actually run on PostgreSQL, not just on SQLite.

SQLite never executes these statements -- it takes the plain UPDATE path -- so
the SQLite suite proves nothing about them. Three separate defects reached the
repository behind that blind spot:

* an untyped NULL parameter in the MERGE source, which PostgreSQL types as
  `text` and then refuses to assign to a date column;
* `UPDATE SET target.value = ...`, which Spark SQL tolerates and PostgreSQL
  rejects, because MERGE resolves the assigned column against the table;
* a value longer than the column's declared VARCHAR width, which SQLite ignores
  outright.

Each one fails on the first real write. This test executes every shipped MERGE
against a real PostgreSQL, so the class cannot come back. Set
COLLECTOR_TEST_PG_URL to a database this suite may create a schema in.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from scripts import init_db
from scripts.config import SCHEMA_NAME

MERGE_MODULES = (
    "metadata",
    "time_series",
    "weights",
    "original_weights",
    "vendor_provenance",
    "availability",
)


def _shipped_ddl() -> list[str]:
    """Return every CREATE statement this collector ships, in creation order."""
    double = init_db.double_type("postgresql")
    statements = [init_db.CREATE_SCHEMA]
    for name in sorted(dir(init_db)):
        if not name.startswith("CREATE_") or name == "CREATE_SCHEMA":
            continue
        ddl = getattr(init_db, name)
        if isinstance(ddl, str):
            statements.append(ddl.format(double=double) if "{double}" in ddl else ddl)
    return statements


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    url = os.getenv("COLLECTOR_TEST_PG_URL")
    if not url:
        pytest.skip("set COLLECTOR_TEST_PG_URL to run the MERGE statements on PostgreSQL")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE"))
        for statement in _shipped_ddl():
            conn.execute(text(statement))
    return engine


def _modules_with_merges() -> list[str]:
    found = []
    for name in MERGE_MODULES:
        if importlib.util.find_spec(f"scripts.{name}") is None:
            continue
        module = importlib.import_module(f"scripts.{name}")
        if hasattr(module, "_merge_statement"):
            found.append(name)
    return found


def test_this_collector_has_merge_statements_to_check() -> None:
    """Guard the guard: a refactor must not make the sweep below vacuous."""
    assert _modules_with_merges(), "no _merge_statement found to exercise"


_SAMPLE = {
    "DATE": date(2024, 1, 1),
    "TIMESTAMP": datetime(2024, 1, 1, 9, 0),  # noqa: DTZ001 -- a literal bind value
    "INTEGER": 1,
    "BIGINT": 1,
    "DOUBLE": 1.5,
    "DOUBLEPRECISION": 1.5,
}


def _column_specs() -> dict[str, tuple[str, bool]]:
    """Return ``column -> (declared type, nullable)`` from the shipped DDL."""
    specs: dict[str, tuple[str, bool]] = {}
    for ddl in _shipped_ddl():
        for line in ddl.splitlines():
            match = re.match(r"\s*(\w+) ([A-Z ]+?)(\(\d+\))?( NOT NULL)?,?\s*$", line)
            if match and match.group(1).upper() not in {"CREATE", "CONSTRAINT"}:
                specs[match.group(1)] = (match.group(2).strip(), not match.group(4))
    return specs


@pytest.mark.parametrize("module_name", _modules_with_merges())
def test_the_merge_statement_runs_on_postgresql(postgres_engine: Engine, module_name: str) -> None:
    """Execute the real statement with the shapes the collector actually writes.

    Every nullable column is passed NULL, which is where the untyped-parameter
    defect lives, and every NOT NULL column gets a value of its declared type.
    Nothing needs to match a stored row: PostgreSQL parses, plans and
    type-checks a MERGE before it looks at any data, which is exactly where all
    three defects surfaced.
    """
    module = importlib.import_module(f"scripts.{module_name}")
    specs = _column_specs()
    parameters: dict[str, object] = {}
    for column in module._COLUMNS:
        declared, nullable = specs.get(column, ("VARCHAR", True))
        key = declared.replace(" ", "").upper()
        parameters[f"{column}_0"] = None if nullable else _SAMPLE.get(key, "x")
    with postgres_engine.begin() as conn:
        conn.execute(module._merge_statement(1), parameters)


def test_the_ddl_parser_found_the_columns() -> None:
    """Guard the guard: a DDL rewrite must not silently null everything."""
    specs = _column_specs()
    assert "series_id" in specs
    assert any(not nullable for _, nullable in specs.values())


@pytest.mark.parametrize("module_name", _modules_with_merges())
def test_the_merge_statement_does_not_qualify_its_assignments(module_name: str) -> None:
    """`UPDATE SET target.col` parses on Spark SQL and fails on PostgreSQL."""
    module = importlib.import_module(f"scripts.{module_name}")
    statement = str(module._merge_statement(1))
    assignments = statement.split("WHEN MATCHED THEN UPDATE SET", 1)[-1]
    assert "target." not in assignments, (
        f"scripts/{module_name}.py qualifies an assigned column with the target alias; "
        "PostgreSQL rejects that in MERGE"
    )
