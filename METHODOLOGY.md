# Methodology — collector_cbi_uk

This repository belongs to the UK inflation predictor fleet. It collects raw
explanatory variables (X) only. Forecast targets (Y), CPI weights and bottom-up
reconciliation remain owned by `collector_ons_cpi` / `collector_ons_ex_cpi`.

## Publisher and delivery provider

The **Confederation of British Industry** runs and publishes the Distributive
Trades, Industrial Trends and Service Sector surveys. That is the economic fact
this collector stores.

**Bloomberg** and **LSEG** are licensed delivery providers: how the desk lawfully
obtains the balances, not their author. The distinction is enforced in code, not
only in prose — `metadata.original_publisher` must name CBI, and
`scripts/metadata.py` rejects a row that names a delivery provider there.
Provider identity lives in `metadata.delivery_provider`,
`metadata.vendor_series_id`, `metadata.vendor_field` and in the vendor columns of
`source_snapshots`.

This is why there is no `collector_bloomberg_uk` and no `collector_reuters_uk`. A
collector is named for a publisher; a vendor is a route, and routes are recorded
per observation rather than given a repository.

## Status vocabulary

The earlier fleet status for this source was `blocked_license`, which asserted
that lawful collection was impossible. That is too coarse — these six facts are
separable, and the status names which are still open:

| Term | Meaning | Here |
| --- | --- | --- |
| `SOURCE_EXISTS` | The publisher publishes the statistic. | yes |
| `VENDOR_AVAILABLE` | A licensed delivery provider is available to the desk. | yes — Bloomberg and/or LSEG |
| `ENTITLEMENT_UNKNOWN` | Whether the account may read this data is unverified. | **open** |
| `VENDOR_SERIES_ID_UNKNOWN` | The vendor identifiers are not confirmed. | **open** |
| `IMPLEMENTATION_READY` | Code, schema, tests and PIT handling are complete. | yes |
| `LIVE_CERTIFICATION_PENDING` | No live vendor query has been run. | **open** |

`ready_with_environment_gate` is the status when `IMPLEMENTATION_READY` holds and
the only open items are environment-bound. `pending_vendor_entitlement` would
apply if entitlement were known to be absent. `implemented_verified` requires a
real query against a real provider and is **not** claimed.

## Unit vocabulary

`scripts/metadata.py` validates every series against a controlled `UNITS` set,
and this repository's copy carries one member the fleet-canonical set does not:
`balance`.

The canonical set, as shipped in `collector_predictor_template`, is `index`,
`percent`, `ratio`, `persons`, `currency`, `count`, `tons`, `hectares`,
`cubic_meters`, `megawatt_hours`, `other`.

A CBI reading is a weighted net balance — the share of respondents reporting an
increase minus the share reporting a decrease, in percentage points bounded by
−100 and +100. Three candidate resolutions exist:

| Option | Consequence |
| --- | --- |
| Map to `percent` | Silently wrong. A net balance is a difference of two percentages; a consumer that averages, compounds or annualises it as a rate produces nonsense. |
| Map to `other` | Truthful but lossy. It discards the sign convention and the bounded scale, which are the properties that make these series usable as predictors. |
| Extend the authority with `balance` | Correct, and reusable — any diffusion-index or net-balance survey in the fleet (CBI, and the BoE DMP and ONS BICS surveys) needs the same member. |

This repository holds the third option locally and declares it here rather than
resolving it by fiat. **While this divergence stands, the collector is not
fleet-vocabulary conformant** and must not be reported as such, regardless of
the test suite passing — the suite validates against the local set.

Reconciling this is an authority decision: the canonical `UNITS` set in
`guimasuko/collector_template` must either gain `balance` or rule it out, and
this file follows that decision.

Note that `collector_brc_uk` does **not** share this problem. The BRC Shop
Price Index is a year-on-year percentage change, and that collector correctly
emits `percent`, which is already canonical.

## What is collected

27 published CBI balances across three surveys, described in the README. The
scope is the price and cost side plus the demand context needed to read it,
rather than the whole survey programme.

Nothing is derived. This collector computes no diffusion index, no three-month
average and no seasonal adjustment. Those are research transformations and belong
in `uk_inflation_predictors`, alongside the lags and features the research layer
builds for every predictor. For survey balances, `last` and lags are the natural
features; a monthly average over a balance that is itself a three-month question
would double-count the window.

## Four dimensions that never collapse

`survey`, `sector`, `measure` (current vs expected) and `stance` are stored as
columns, not merely encoded in the identifier, so a query can select "every
expected selling-price balance across the three surveys" without a LIKE pattern.

The `measure` split matters most. CBI's current balance asks what happened over
the past three months; the expected balance asks about the next three. Storing
them as one series would put a forward-looking number in a realised series — a
look-ahead built into the data model, which no point-in-time machinery
downstream could undo.

## The five tables

`metadata`, `time_series`, `availability`, `source_snapshots`, `logs` — the fleet
shape, in a schema named `collector_cbi_uk`.

Two documented deviations, forced by the delivery model rather than chosen:

1. `metadata` carries vendor provenance and survey dimensions
   (`original_publisher`, `delivery_provider`, `vendor_series_id`,
   `vendor_field`, `vendor_description`, `survey`, `sector`, `measure`,
   `category`, `stance`, `seasonal_adjustment`, `reference_date_rule`,
   `release_rule`, `revision_policy`, `license_context`, `history_start`), plus
   the fleet's `source_id`.
2. `source_snapshots` carries `delivery_provider`, `vendor_series_id`,
   `vendor_field`, `vendor_query` and `vendor_row_count`, because `source_url`
   cannot identify an API response.

Together these let any stored observation be traced back to: *this CBI balance,
from this survey and sector, delivered by this provider under this identifier and
field, over this query window, retrieved at this instant, first knowable at this
instant on this basis, from a response with this digest.*

## Snapshots without bytes

A public-file collector hashes the file it downloaded. There is no file here: a
licensed provider answers with objects over an API. Snapshot identity is the
SHA-256 of a canonical, deterministic serialization of the request and the rows
it returned — sorted keys, sorted rows, stable separators.

The retrieval instant is deliberately **excluded** from the digest. Including it
would give every rerun a new snapshot id, so the run that should have written
nothing would write a fresh snapshot row for every series every day, and the
table would stop being evidence of change. The instant is still recorded, as
`source_snapshots.fetched_at`, where it is evidence rather than identity.

## Validation before persistence

Every provider response is validated in `scripts/normalize.py` before anything is
written, and each failure is loud rather than a dropped row:

- **Empty history is an error, never an empty success.** This is the failure mode
  a vendor-delivered collector must not ship. An entitlement failure, an unknown
  identifier and a genuinely empty window all look like "no rows" unless they are
  separated, and a collector that shrugs at no rows silently stops tracking a
  series forever. `scripts/vendor_errors.py` gives each state its own exception.
- **Period stamps.** A monthly balance may be stamped at either end of its month;
  a quarterly one at either end of its quarter. Both collapse to the period's
  first day, so a provider using period-end stamps and one using period-start
  stamps agree. **The catalog, not the response, decides which rule applies** — a
  series' frequency is an economic fact about the CBI survey, and inferring it
  from whatever the vendor returned would let a wrong identifier define its own
  period convention. A mid-period stamp is rejected: for a quarterly series,
  2024-02-29 is a month end, and accepting it would quietly relabel a mid-quarter
  value as Q1.
- **Duplicates.** The same period twice with the same value collapses. With two
  different values, the run fails.
- **Canonical ids.** Only ids in `scripts/series_catalog.py` can be stored.

## Two providers, one series

Both adapters converge on `VendorSeriesResponse`, and `tests/test_normalize.py`
asserts that a Bloomberg response and an LSEG response carrying the same history
produce identical canonical observations — including when one uses period-end
stamps and the other period-start, for both frequencies.

Roles are strict. The **primary** supplies every stored value. The **fallback**
is used only when the primary is *unreachable*; an entitlement failure or an
unknown identifier is never failed over, because both mean the configuration is
wrong and switching vendors would hide that behind plausible data. The
**cross-check** is read and compared but never written, and a material
disagreement fails the run: two licensed routes to one CBI balance returning
different numbers means one of the two identifiers is wrong — quite possibly
pointing at a different survey.

## Idempotency and revisions

An unchanged rerun writes no `time_series`, `availability` or `source_snapshots`
rows and leaves `metadata` untouched; only the run log changes. A later-day
change to a stored period inserts a new vintage and preserves the old one. A
same-day change fails closed, because the fleet key stores `vintage_date` as a
DATE and cannot represent two intraday information sets without rewriting
history. All three runs are asserted in `tests/test_persistence.py`.

## Timezone

CBI publishes in London. Naive vendor timestamps are interpreted in
`Europe/London` through the tz database and converted to UTC, so GMT and BST are
both handled by the date itself. A fixed offset would be wrong for half the year,
and is never used.

## Isolation

No module imports another collector, `uk_inflation_predictors`, or a shared
package. There is no `BaseCollector`, no vendor framework, no `core/` and no
subpackage under `scripts/`. There is deliberately **no shared vendor package
between `collector_brc_uk` and this repository**: the Bloomberg and LSEG adapters
are copied, as the fleet requires, so each collector stays independently
deployable and auditable. `tests/test_registry_and_architecture.py` parses every
file in the repository and fails if any of that appears.
