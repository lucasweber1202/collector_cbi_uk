"""The controlled vocabularies must not drift from the fleet-canonical sets.

This collector previously carried a local UNITS set with an extra member,
"balance", so its own validation accepted a unit no other repository in the
fleet would. The suite passed precisely because it validated against the
widened local copy. These tests pin the canonical membership itself, so
widening the set fails the build rather than quietly legalising a new unit.
"""

from __future__ import annotations

from scripts.metadata import UNITS
from scripts.series_catalog import ALL_SERIES

# The fleet-canonical unit vocabulary. Adding a member here is a change to the
# authority (guimasuko/collector_template), not a change to this repository.
CANONICAL_UNITS = frozenset(
    {
        "index",
        "percent",
        "ratio",
        "persons",
        "currency",
        "count",
        "tons",
        "hectares",
        "cubic_meters",
        "megawatt_hours",
        "other",
    }
)


def test_units_match_the_canonical_vocabulary_exactly() -> None:
    assert UNITS - CANONICAL_UNITS == set(), f"local extensions: {UNITS - CANONICAL_UNITS}"
    assert CANONICAL_UNITS - UNITS == set(), f"missing members: {CANONICAL_UNITS - UNITS}"


def test_no_series_declares_balance_as_its_unit() -> None:
    """ "balance" is not canonical; it survives as metadata.stance instead."""
    assert [s.series_id for s in ALL_SERIES if s.unit == "balance"] == []


def test_every_series_declares_a_canonical_unit() -> None:
    offenders = {s.series_id: s.unit for s in ALL_SERIES if s.unit not in CANONICAL_UNITS}
    assert offenders == {}, f"non-canonical units: {offenders}"


def test_net_balance_semantics_survive_outside_the_unit_field() -> None:
    """Publishing "other" is only acceptable because the meaning is kept.

    stance carries the net-balance convention into the sidecar, and the
    description says so in words, so a consumer is never left with a bare
    "other" and no way to learn what the number is.
    """
    for series in ALL_SERIES:
        assert series.stance == "balance", series.series_id
        assert "balance" in series.description.lower(), series.series_id
