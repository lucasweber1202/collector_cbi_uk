"""The vendor registry, the survey catalog, and the standalone guarantee."""

from __future__ import annotations

import ast
import csv
from pathlib import Path

import pytest

from scripts.config import PROVIDERS, SCHEMA_NAME, VENDOR_SERIES_FILE
from scripts.series_catalog import (
    ALL_SERIES,
    DISTRIBUTIVE_TRADES,
    INDUSTRIAL_TRENDS,
    PRIORITY_PRICE_SERIES,
    SERIES_BY_ID,
    SERVICE_SECTOR,
    SURVEYS,
    describe_series_id,
    parse_series_id,
)
from scripts.vendor_registry import FIELDNAMES, PENDING, load_registry, pending_for, resolved_for

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_every_survey_has_a_current_and_an_expected_price_balance() -> None:
    """Selling prices and expected selling prices are the maximum-priority pair."""
    for survey, category in (
        (DISTRIBUTIVE_TRADES, "selling_prices"),
        (INDUSTRIAL_TRENDS, "selling_prices"),
        (SERVICE_SECTOR, "prices_charged"),
    ):
        measures = {
            series.measure
            for series in ALL_SERIES
            if series.survey == survey and series.category == category
        }
        assert measures == {"current", "expected"}, (survey, category)


def test_the_three_surveys_are_all_represented() -> None:
    assert {series.survey for series in ALL_SERIES} == set(SURVEYS)


def test_priority_price_series_are_the_price_and_cost_balances() -> None:
    """The discovery command starts from the balances this collector exists for."""
    assert {series.category for series in PRIORITY_PRICE_SERIES} == {
        "selling_prices",
        "prices_charged",
        "average_costs",
    }
    assert len(PRIORITY_PRICE_SERIES) < len(ALL_SERIES)


def test_current_and_expected_are_never_the_same_series() -> None:
    """A forward-looking balance is a different economic object from a realised one."""
    for series in ALL_SERIES:
        twin = [
            other
            for other in ALL_SERIES
            if other.survey == series.survey
            and other.sector == series.sector
            and other.category == series.category
            and other.measure != series.measure
        ]
        for other in twin:
            assert other.series_id != series.series_id


def test_service_sectors_stay_separate_populations() -> None:
    """Business/professional and consumer services are not merged into one series."""
    sectors = {series.sector for series in ALL_SERIES if series.survey == SERVICE_SECTOR}
    assert sectors == {"business_professional", "consumer"}


def test_surveys_are_never_collapsed_into_one_selling_price_series() -> None:
    """'CBI selling prices' must resolve to three different series, not one."""
    price_ids = {
        series.series_id
        for series in ALL_SERIES
        if series.category == "selling_prices" and series.measure == "expected"
    }
    assert len(price_ids) == 2  # distributive trades and industrial trends
    assert len({series.survey for series in ALL_SERIES if series.series_id in price_ids}) == 2


def test_series_ids_are_economic_and_carry_no_vendor_identity() -> None:
    """A canonical id must not change because the delivery provider changed."""
    for series in ALL_SERIES:
        upper = series.series_id.upper()
        assert series.series_id == upper
        for vendor_token in ("BLOOMBERG", "LSEG", "REFINITIV", "REUTERS"):
            assert vendor_token not in upper


def test_every_series_id_decomposes_to_its_catalog_facts() -> None:
    for series in ALL_SERIES:
        publisher, survey, sector, category, measure = describe_series_id(series.series_id)
        assert publisher == "CBI"
        assert (survey, sector, category, measure) == (
            series.survey,
            series.sector,
            series.category,
            series.measure,
        )


def test_the_publisher_is_brc_for_every_series() -> None:
    """Bloomberg and LSEG are routes. They are never recorded as the publisher."""
    for series in ALL_SERIES:
        fields = series.metadata_fields()
        assert fields["original_publisher"] == "Confederation of British Industry"


def test_series_ids_are_unique() -> None:
    assert len(SERIES_BY_ID) == len(ALL_SERIES)


# ---------------------------------------------------------------------------
# Vendor registry
# ---------------------------------------------------------------------------


def test_no_vendor_identifier_is_invented() -> None:
    """The single most important test in this repository.

    Every shipped identifier is the explicit sentinel. A plausible-looking
    ticker committed here would either fail loudly on an entitled machine or,
    far worse, resolve to a different statistic and poison the stored history.
    """
    registry = load_registry()
    for mapping in registry.values():
        assert mapping.identifier == PENDING, (
            f"{mapping.series_id}/{mapping.provider} ships identifier "
            f"{mapping.identifier!r}. Identifiers are recorded only after "
            "confirmation inside an entitled session."
        )
        assert mapping.field == PENDING
        assert mapping.confirmed_on is None


def test_every_series_has_a_row_for_every_provider() -> None:
    registry = load_registry()
    for series_id in SERIES_BY_ID:
        for provider in PROVIDERS:
            assert (series_id, provider) in registry


def test_the_registry_file_has_exactly_the_declared_columns() -> None:
    with VENDOR_SERIES_FILE.open(encoding="utf-8", newline="") as handle:
        assert tuple(csv.reader(handle).__next__()) == FIELDNAMES


def test_nothing_is_resolved_yet_and_everything_is_pending(tmp_path: Path) -> None:
    registry = load_registry()
    for provider in PROVIDERS:
        assert resolved_for(provider, registry) == []
        assert len(pending_for(provider, registry)) == len(SERIES_BY_ID)


def _write_registry(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "vendor_series.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(**overrides: str) -> dict[str, str]:
    base = {
        "series_id": "CBI_DISTRIBUTIVE_TRADES_SELLING_PRICES_EXPECTED",
        "provider": "bloomberg",
        "identifier": PENDING,
        "field": PENDING,
        "vendor_description": "",
        "history_start": "",
        "confirmed_on": "",
        "notes": "",
    }
    base.update(overrides)
    return base


def test_an_identifier_without_a_confirmation_date_is_refused(tmp_path: Path) -> None:
    """A ticker is recorded only together with the day someone verified it."""
    path = _write_registry(tmp_path, [_row(identifier="SOMETHING Index", field="PX_LAST")])
    with pytest.raises(ValueError, match="confirmed_on"):
        load_registry(path)


def test_an_unknown_series_id_is_refused(tmp_path: Path) -> None:
    """A typo must not present as an honest pending entry."""
    path = _write_registry(tmp_path, [_row(series_id="CBI_SOMETHING_ELSE")])
    with pytest.raises(ValueError, match="not a canonical CBI series"):
        load_registry(path)


def test_an_unknown_provider_is_refused(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_row(provider="macrobond")])
    with pytest.raises(ValueError, match="unknown provider"):
        load_registry(path)


def test_a_blank_identifier_is_refused(tmp_path: Path) -> None:
    """Blank is ambiguous; the sentinel is explicit."""
    path = _write_registry(tmp_path, [_row(identifier="")])
    with pytest.raises(ValueError, match="must state an identifier"):
        load_registry(path)


def test_a_missing_series_is_refused(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_row()])
    with pytest.raises(ValueError, match="missing rows"):
        load_registry(path)


def test_a_duplicate_row_is_refused(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_row(), _row()])
    with pytest.raises(ValueError, match="duplicate row"):
        load_registry(path)


def test_a_resolved_mapping_is_reported_as_resolved(tmp_path: Path) -> None:
    """The deployment step: fill the cells in, and the series becomes collectable."""
    rows = [
        _row(
            series_id=series_id,
            provider=provider,
            identifier="TEST Index" if provider == "bloomberg" else "TEST=ECI",
            field="PX_LAST" if provider == "bloomberg" else "VALUE",
            confirmed_on="2026-09-17",
        )
        for series_id in SERIES_BY_ID
        for provider in PROVIDERS
    ]
    registry = load_registry(_write_registry(tmp_path, rows))
    assert len(resolved_for("bloomberg", registry)) == len(SERIES_BY_ID)
    assert pending_for("lseg", registry) == []


# ---------------------------------------------------------------------------
# Standalone guarantee
# ---------------------------------------------------------------------------

FORBIDDEN_PREFIXES = ("collector_", "uk_inflation_predictors")
_OWN_PACKAGE = SCHEMA_NAME


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import cannot reach a sibling repository
                continue
            if node.module:
                modules.add(node.module)
    return modules


def _python_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.py")
        if ".venv" not in path.parts and "_raw" not in path.parts
    ]


def test_no_module_imports_another_repository() -> None:
    """Each collector must be independently deployable and auditable."""
    offenders: list[str] = []
    for path in _python_files():
        for module in _imported_modules(path):
            root = module.split(".")[0]
            if root == _OWN_PACKAGE:
                offenders.append(f"{path.relative_to(ROOT)} imports itself as a package: {module}")
            elif root.startswith(FORBIDDEN_PREFIXES):
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not offenders, offenders


def test_there_is_no_shared_vendor_package_or_base_class() -> None:
    """The fleet forbids a cross-repository core, a BaseCollector or a plugin system."""
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert not node.name.startswith("Base"), (
                    f"{path.relative_to(ROOT)} defines {node.name}; the fleet is flat and "
                    "explicit, with no base classes or adapter framework."
                )


def test_scripts_is_a_flat_module_directory() -> None:
    """No core/, lib/, common/ or subpackage inside scripts/."""
    subdirectories = [
        entry.name
        for entry in (ROOT / "scripts").iterdir()
        if entry.is_dir() and entry.name != "__pycache__"
    ]
    assert subdirectories == []


def test_every_series_id_round_trips(_: None = None) -> None:
    """The fleet contract: build_series_id(*parse_series_id(sid)) == sid."""
    from scripts.extract import build_series_id

    for series in ALL_SERIES:
        assert build_series_id(*parse_series_id(series.series_id)) == series.series_id
