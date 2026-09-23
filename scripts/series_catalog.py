"""The canonical CBI series this collector owns.

The scope is deliberately narrow. CBI runs a large survey programme, and
collecting all of it indiscriminately would bury the handful of balances that
actually bear on UK inflation. What is defined here is the price and cost side
of three surveys, plus the demand context needed to read it.

The identifiers are economic, not vendor identifiers. A series keeps the same
`series_id` whether Bloomberg or LSEG delivered it; the delivery route is
provenance and lives in config/vendor_series.csv and in the vendor columns of
`metadata` and `source_snapshots`.

Four dimensions are kept distinct and are never collapsed, because collapsing
any of them would silently merge different questions asked of different
populations:

survey
    `distributive_trades`, `industrial_trends`, `service_sector`. Three separate
    surveys with separate samples, questionnaires and release calendars. They
    are not stitched, and "CBI selling prices" is never one series.
sector
    Within the Service Sector Survey, CBI reports business and professional
    services separately from consumer services. They are different populations
    and stay different series.
measure
    `current` (what happened over the past three months) and `expected` (what
    respondents expect over the next three). A forward-looking balance is a
    different economic object from a realised one; merging them would be a
    look-ahead baked into the series definition.
stance
    `balance` for a net percentage balance, `level` where CBI reports a level.

Everything here is a *published* CBI balance. This collector never computes a
diffusion index, a three-month average or a seasonal adjustment; those are
research transformations and belong in `uk_inflation_predictors`.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.config import ORIGINAL_PUBLISHER, PUBLISHER_URL, SOURCE_ID

DISTRIBUTIVE_TRADES = "distributive_trades"
INDUSTRIAL_TRENDS = "industrial_trends"
SERVICE_SECTOR = "service_sector"

CURRENT = "current"
EXPECTED = "expected"

# CBI publishes weighted percentage balances. The headline balances are reported
# seasonally adjusted; this collector records that as metadata rather than
# performing any adjustment of its own, and `unknown` is used wherever the
# published basis has not been confirmed against the vendor's own description.
SEASONAL_ADJUSTMENT = "sa"

REFERENCE_DATE_RULE = (
    "reference_date is the first day of the survey period the balance describes: "
    "the survey month for the monthly Distributive Trades and Industrial Trends "
    "balances, and the first month of the survey quarter for quarterly balances."
)
REVISION_POLICY = (
    "CBI publishes no revision policy for its survey balances. Restatements are "
    "handled generically: a changed value for a stored reference period becomes "
    "a new vintage and never overwrites the previous one."
)
LICENSE_CONTEXT = (
    "CBI survey data is licensed. It is obtained here through a licensed "
    "delivery provider (Bloomberg or LSEG) under the desk's own entitlement, "
    "stored for internal research only, and never redistributed."
)

RELEASE_RULES = {
    DISTRIBUTIVE_TRADES: (
        "Distributive Trades Survey: monthly, released towards the end of the "
        "survey month. The exact instant is taken from the provider when the "
        "provider supplies one and is never assumed."
    ),
    INDUSTRIAL_TRENDS: (
        "Industrial Trends Survey: monthly balances each month, with the fuller "
        "quarterly round in January, April, July and October. The exact instant "
        "is taken from the provider when the provider supplies one."
    ),
    SERVICE_SECTOR: (
        "Service Sector Survey: quarterly, released towards the end of the "
        "survey quarter. The exact instant is taken from the provider when the "
        "provider supplies one."
    ),
}

FREQUENCIES = {
    DISTRIBUTIVE_TRADES: "monthly",
    INDUSTRIAL_TRENDS: "monthly",
    SERVICE_SECTOR: "quarterly",
}


@dataclass(frozen=True)
class SeriesDefinition:
    """One canonical CBI balance, independent of any delivery provider."""

    series_id: str
    name: str
    description: str
    survey: str
    sector: str
    category: str
    measure: str
    stance: str
    unit: str
    frequency: str
    eco_group: str
    priority: str

    def metadata_fields(self) -> dict[str, object]:
        """Return the catalog half of this series' metadata row."""
        return {
            "source_id": SOURCE_ID,
            "name": self.name,
            "description": self.description,
            "frequency": self.frequency,
            "unit": self.unit,
            "eco_group": self.eco_group,
            "source_url": PUBLISHER_URL,
            "seasonal_adjustment": SEASONAL_ADJUSTMENT,
            "original_publisher": ORIGINAL_PUBLISHER,
            "survey": self.survey,
            "sector": self.sector,
            "measure": self.measure,
            "category": self.category,
            "stance": self.stance,
            "reference_date_rule": REFERENCE_DATE_RULE,
            "release_rule": RELEASE_RULES[self.survey],
            "revision_policy": REVISION_POLICY,
            "license_context": LICENSE_CONTEXT,
        }


def _series(
    survey: str,
    survey_label: str,
    category: str,
    category_label: str,
    measure: str,
    priority: str,
    sector: str = "total",
    sector_label: str = "",
    eco_group: str = "business_confidence",
    stance: str = "balance",
) -> SeriesDefinition:
    """Build one published CBI balance."""
    horizon = "over the past three months" if measure == CURRENT else "over the next three months"
    scope = f" ({sector_label})" if sector_label else ""
    prefix = "Expected " if measure == EXPECTED else ""
    sector_part = f"_{sector.upper()}" if sector != "total" else ""
    return SeriesDefinition(
        series_id=f"CBI_{survey.upper()}{sector_part}_{category.upper()}_{measure.upper()}",
        name=f"CBI {survey_label}{scope}, {prefix.lower() or ''}{category_label}".strip().replace(
            " ,", ","
        ),
        description=(
            f"Confederation of British Industry {survey_label}{scope}: weighted "
            f"percentage balance for {category_label} {horizon}, as published in "
            "the survey release."
        ),
        survey=survey,
        sector=sector,
        category=category,
        measure=measure,
        stance=stance,
        unit="other",
        frequency=FREQUENCIES[survey],
        eco_group=eco_group,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Priority A — Distributive Trades Survey
#
# Selling prices and expected selling prices are the maximum-priority balances:
# they are the retail price question, asked of retailers, ahead of the CPI goods
# print. Sales, orders and stocks are collected as the demand context needed to
# read a price balance, not for their own sake.
# ---------------------------------------------------------------------------

DISTRIBUTIVE_TRADES_SERIES: tuple[SeriesDefinition, ...] = (
    _series(
        DISTRIBUTIVE_TRADES,
        "Distributive Trades Survey",
        "selling_prices",
        "retail selling prices",
        CURRENT,
        "A1",
        eco_group="consumer_prices",
    ),
    _series(
        DISTRIBUTIVE_TRADES,
        "Distributive Trades Survey",
        "selling_prices",
        "retail selling prices",
        EXPECTED,
        "A1",
        eco_group="inflation_expectations",
    ),
    _series(
        DISTRIBUTIVE_TRADES,
        "Distributive Trades Survey",
        "retail_sales",
        "retail sales volume",
        CURRENT,
        "A2",
        eco_group="retail_sales",
    ),
    _series(
        DISTRIBUTIVE_TRADES,
        "Distributive Trades Survey",
        "retail_sales",
        "retail sales volume",
        EXPECTED,
        "A2",
        eco_group="retail_sales",
    ),
    _series(
        DISTRIBUTIVE_TRADES,
        "Distributive Trades Survey",
        "orders_on_suppliers",
        "orders placed upon suppliers",
        CURRENT,
        "A2",
        eco_group="retail_sales",
    ),
    _series(
        DISTRIBUTIVE_TRADES,
        "Distributive Trades Survey",
        "orders_on_suppliers",
        "orders placed upon suppliers",
        EXPECTED,
        "A2",
        eco_group="retail_sales",
    ),
    _series(
        DISTRIBUTIVE_TRADES,
        "Distributive Trades Survey",
        "stock_adequacy",
        "stocks in relation to expected demand",
        CURRENT,
        "A3",
        eco_group="retail_sales",
    ),
)

# ---------------------------------------------------------------------------
# Priority B — Industrial Trends Survey
#
# Again selling prices and expected selling prices first: this is the upstream
# goods price question. Costs are collected where CBI publishes them, because a
# cost balance is the pressure behind a price balance. Capacity utilisation is
# deliberately absent: it is a capacity measure, not a price measure, and no
# inflation justification for it was established.
# ---------------------------------------------------------------------------

INDUSTRIAL_TRENDS_SERIES: tuple[SeriesDefinition, ...] = (
    _series(
        INDUSTRIAL_TRENDS,
        "Industrial Trends Survey",
        "selling_prices",
        "domestic selling prices",
        CURRENT,
        "B1",
        eco_group="producer_prices",
    ),
    _series(
        INDUSTRIAL_TRENDS,
        "Industrial Trends Survey",
        "selling_prices",
        "domestic selling prices",
        EXPECTED,
        "B1",
        eco_group="inflation_expectations",
    ),
    _series(
        INDUSTRIAL_TRENDS,
        "Industrial Trends Survey",
        "average_costs",
        "average unit costs",
        CURRENT,
        "B2",
        eco_group="producer_prices",
    ),
    _series(
        INDUSTRIAL_TRENDS,
        "Industrial Trends Survey",
        "average_costs",
        "average unit costs",
        EXPECTED,
        "B2",
        eco_group="producer_prices",
    ),
    _series(
        INDUSTRIAL_TRENDS,
        "Industrial Trends Survey",
        "total_orders",
        "total order books",
        CURRENT,
        "B3",
        eco_group="production",
    ),
    _series(
        INDUSTRIAL_TRENDS,
        "Industrial Trends Survey",
        "export_orders",
        "export order books",
        CURRENT,
        "B3",
        eco_group="production",
    ),
    _series(
        INDUSTRIAL_TRENDS,
        "Industrial Trends Survey",
        "output",
        "volume of output",
        CURRENT,
        "B3",
        eco_group="production",
    ),
    _series(
        INDUSTRIAL_TRENDS,
        "Industrial Trends Survey",
        "output",
        "volume of output",
        EXPECTED,
        "B3",
        eco_group="production",
    ),
)

# ---------------------------------------------------------------------------
# Priority C — Service Sector Survey
#
# Business and professional services and consumer services are separate
# populations and stay separate series. This survey is quarterly and its
# published history via a vendor is the least certain of the three, so no
# implementation is forced: a series whose provider history turns out to be
# discontinuous stays pending rather than being stitched.
# ---------------------------------------------------------------------------

_SERVICE_SECTORS = (
    ("business_professional", "business and professional services"),
    ("consumer", "consumer services"),
)

SERVICE_SECTOR_SERIES: tuple[SeriesDefinition, ...] = tuple(
    _series(
        SERVICE_SECTOR,
        "Service Sector Survey",
        category,
        category_label,
        measure,
        priority,
        sector=sector,
        sector_label=sector_label,
        eco_group=eco_group,
    )
    for sector, sector_label in _SERVICE_SECTORS
    for category, category_label, measure, priority, eco_group in (
        ("prices_charged", "average selling prices", CURRENT, "C1", "consumer_prices"),
        ("prices_charged", "average selling prices", EXPECTED, "C1", "inflation_expectations"),
        ("average_costs", "average costs per person employed", CURRENT, "C2", "producer_prices"),
        ("average_costs", "average costs per person employed", EXPECTED, "C2", "producer_prices"),
        (
            "business_situation",
            "optimism about the business situation",
            CURRENT,
            "C3",
            "business_confidence",
        ),
        ("employment", "numbers employed", EXPECTED, "C3", "employment"),
    )
)

ALL_SERIES: tuple[SeriesDefinition, ...] = (
    DISTRIBUTIVE_TRADES_SERIES + INDUSTRIAL_TRENDS_SERIES + SERVICE_SECTOR_SERIES
)
SERIES_BY_ID: dict[str, SeriesDefinition] = {series.series_id: series for series in ALL_SERIES}

SURVEYS = (DISTRIBUTIVE_TRADES, INDUSTRIAL_TRENDS, SERVICE_SECTOR)

# The price and cost balances this collector exists for. Everything else is
# demand context. Used by the discovery command so the corporate-machine step
# starts with the series that matter.
PRIORITY_PRICE_SERIES: tuple[SeriesDefinition, ...] = tuple(
    series
    for series in ALL_SERIES
    if series.category in {"selling_prices", "prices_charged", "average_costs"}
)


def describe_series_id(series_id: str) -> tuple[str, str, str, str, str]:
    """Decompose a canonical id into (publisher, survey, sector, category, measure)."""
    definition = SERIES_BY_ID.get(series_id)
    if definition is None:
        raise ValueError(f"{series_id} is not a canonical CBI series")
    return (
        "CBI",
        definition.survey,
        definition.sector,
        definition.category,
        definition.measure,
    )


def parse_series_id(series_id: str) -> tuple[str, ...]:
    """Split a canonical id into its raw underscore components.

    This is the fleet contract (GUIDELINES.md 4): uppercase, underscore
    separated, ordered coarse -> fine, and exactly reversible, so
    build_series_id(*parse_series_id(sid)) == sid. The semantic view -- which
    survey, category and measure an id denotes -- is describe_series_id, and
    the catalog itself carries those facts.
    """
    if series_id not in SERIES_BY_ID:
        raise ValueError(f"{series_id} is not a canonical CBI series")
    return tuple(series_id.split("_"))
