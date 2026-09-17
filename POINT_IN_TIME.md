# Point-in-time contract — collector_cbi_uk

A historical backtest must never see information that was unavailable at the
simulated forecast instant. For a vendor-delivered series this is easy to get
wrong in a way that looks fine, so the rule is stated first:

> **Bloomberg and LSEG will serve decades of CBI survey history in one call today.
> That history is real. The *knowledge* of it is not retroactive.**

Nothing in this repository ever backdates an observation to the month it
describes. A first full-history backfill stamps the entire history `first_seen`
at the instant the collector ran. That is an honest upper bound, and it is the
correct answer rather than a weakness: the desk genuinely does not know when a
2011 balance reached the vendor, and claiming to know would put a look-ahead
into every backtest that ran over it.

Concretely: a balance published at **2026-09-15 00:01 Europe/London** is
invisible to any forecast made before that instant, and `get_series_as_of` enforces it.

## Stored dates

- `reference_date` — the survey period the balance describes, normalised to its
  first day: the first of the survey month for a monthly balance, the first day
  of the survey quarter for a quarterly one.
- `vintage_date` — the UTC date on which this collector stored that version.
- `release_date` — the publication date, when a provider reports one.
- `available_at` — the earliest defensible instant at which this stored vintage
  could have been known.
- `collected_at` — the timestamp of this pipeline run.

`retrieved_at` (when the vendor was queried) is kept too, as
`source_snapshots.fetched_at`.

## availability_basis

| Basis | Meaning here |
| --- | --- |
| `official_timestamp` | The provider reported a machine-readable publication instant for this period. Only a provider-reported instant qualifies. |
| `official_date` | A publication date was reported, to day precision. Stamped 00:01 Europe/London — the earliest defensible instant on a known release day — converted to UTC. |
| `archived_release` | Recovered from an archived copy of the release rather than the live vendor. |
| `first_seen` | No publication instant was reported, so the instant this collector observed the value is used. A true upper bound. |
| `inferred` | Reconstructed from a documented release rule. **This collector never produces it** for vendor-delivered history: a vendor backfill with no timestamp is `first_seen`, not a reconstruction. |
| `unknown` | No defensible basis exists. |

`get_series_as_of()` accepts `official_timestamp`, `official_date`,
`archived_release` and `first_seen` by default. `inferred` and `unknown` require
an explicit opt-in, so reconstructed availability can never be mistaken for a
point-in-time guarantee.

## A release rule is not evidence

Each CBI survey has its own calendar — the Distributive Trades and Industrial
Trends balances monthly, the Service Sector Survey quarterly. Those rules are
documented in `scripts/series_catalog.py` as `release_rule`, per survey — as
*metadata*, so a researcher can read it.

They are never used to stamp `available_at`. A documented rule tells you when a
release was *scheduled*, not when a particular balance actually became
knowable, and a collector that converts a schedule into an availability instant
has invented point-in-time evidence. Attribution goes, strictly in order:
provider-reported instant, then provider-reported date, then `first_seen`. There
is no fourth step.

## Historical revisions

A revised value for an already stored `(series_id, reference_date)` must not
reuse the original publication instant. Unless the provider exposes explicit
evidence for the revision release, the revised vintage is stamped:

- `available_at = collected_at`
- `availability_basis = first_seen`
- `release_date = NULL`

This prevents a 2026 restatement of a 2024 period from appearing in a 2024
backtest, and it is asserted in `tests/test_persistence.py`.

## Same-day revisions

The fleet schema uses `vintage_date DATE`, so two different intraday revisions
cannot be represented without overwriting one information set. This collector
fails closed when an already stored vintage changes again on the same UTC date.
Retry after the UTC date changes rather than rewriting history.

## As-of guarantee

`get_series_as_of(series_id, as_of)` filters on `available_at <= as_of` *before*
ranking vintages, so a later revision cannot mask the vintage that was actually
current at the historical instant. The contract is re-checked in Python before
the rows are returned, because a silent look-ahead is the one failure this
module must never ship.
