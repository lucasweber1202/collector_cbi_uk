"""Parse every shipped SQL statement with Spark's own grammar.

Databricks SQL is Spark SQL, and `pyspark` is already a required dependency of
this collector because `scripts/databricks_engine.py` resolves its token
through `pyspark.dbutils`. Running the statements through the same ANTLR
grammar Databricks uses is therefore free of any credential, and it is the only
check available here that a statement is valid Databricks SQL at all.

This is a grammar check, not an execution: it proves the statements parse, not
that a cluster accepts the plan. Runtime behaviour on a real warehouse remains
unverified from this environment.

It earns its place because the dialects genuinely disagree. PostgreSQL rejects
`DOUBLE` while Databricks rejects `DOUBLE PRECISION`; PostgreSQL rejects an
alias-qualified `SET target.col` in a MERGE while Spark accepts it. A statement
fixed for one engine can silently break the other, and only two of the nine
collectors had a grammar check at all.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Iterator
from typing import Any

import pytest

from scripts import init_db

MERGE_MODULES = (
    "metadata",
    "time_series",
    "weights",
    "original_weights",
    "vendor_provenance",
    "availability",
    "snapshots",
    "run_logs",
)


@pytest.fixture(scope="module")
def spark_parser() -> Iterator[Any]:
    """Yield Spark's SQL parser from a throwaway local session."""
    try:
        from pyspark.sql import SparkSession

        session = (
            SparkSession.builder.master("local[1]")
            .appName("sql-grammar")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.session.timeZone", "UTC")
            .getOrCreate()
        )
    except Exception as exc:  # noqa: BLE001 -- any JVM/py4j failure means no parser
        # pragma: no cover -- no JVM available
        pytest.skip(f"Spark SQL parser unavailable: {exc}")
    try:
        yield session._jsparkSession.sessionState().sqlParser()
    finally:
        session.stop()


def _statements() -> dict[str, str]:
    """Collect every statement this collector sends to a database."""
    double = init_db.double_type("databricks")
    found: dict[str, str] = {}
    for name in sorted(dir(init_db)):
        if name.startswith("CREATE_"):
            ddl = getattr(init_db, name)
            if isinstance(ddl, str):
                found[f"ddl.{name}"] = ddl.format(double=double) if "{double}" in ddl else ddl
    for module_name in MERGE_MODULES:
        if importlib.util.find_spec(f"scripts.{module_name}") is None:
            continue
        module = importlib.import_module(f"scripts.{module_name}")
        for attribute in ("_merge_statement", "_insert_statement"):
            builder = getattr(module, attribute, None)
            if callable(builder):
                found[f"{module_name}{attribute}"] = str(builder(2))
        for attribute in dir(module):
            if attribute.endswith("_SQL") and attribute.startswith("_"):
                value = getattr(module, attribute)
                rendered = str(value)
                if rendered.strip():
                    found[f"{module_name}.{attribute}"] = rendered
    return found


ALL_STATEMENTS = _statements()


def test_there_are_statements_to_parse() -> None:
    """Guard the guard: a refactor must not make the sweep below vacuous."""
    assert ALL_STATEMENTS, "no SQL statements were collected"
    assert any(name.startswith("ddl.") for name in ALL_STATEMENTS)
    assert any("_merge_statement" in name for name in ALL_STATEMENTS)


@pytest.mark.parametrize("name", sorted(ALL_STATEMENTS))
def test_the_statement_is_valid_databricks_sql(spark_parser: Any, name: str) -> None:
    from pyspark.errors import ParseException

    statement = ALL_STATEMENTS[name]
    # SQLAlchemy renders named parameters as :name, which Spark's grammar does
    # not know. They stand in for literals, so a literal is what it parses.
    import re

    literal = re.sub(r":(\w+)", "'x'", statement)
    try:
        spark_parser.parsePlan(literal)
    except Exception as exc:
        if isinstance(exc, ParseException) or "ParseException" in type(exc).__name__:
            pytest.fail(f"{name} is not valid Databricks SQL: {exc}")
        raise


def test_double_precision_is_never_emitted() -> None:
    """PostgreSQL spells it DOUBLE PRECISION; Databricks rejects that spelling."""
    for name, statement in ALL_STATEMENTS.items():
        assert "DOUBLE PRECISION" not in statement.upper(), (
            f"{name} emits DOUBLE PRECISION, which Databricks rejects"
        )
