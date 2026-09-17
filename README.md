# collector_cbi_uk

Standalone collector for **Confederation of British Industry** survey balances,
retrieved through a **licensed delivery provider**.

```
Confederation of British Industry   <- the economic publisher
        |
Bloomberg  and/or  LSEG             <- licensed delivery providers
        |
collector_cbi_uk                    <- this repository
        |
canonical persisted data contract   <- metadata / time_series / availability /
        |                              source_snapshots / logs
uk_inflation_predictors             <- the research layer
```

CBI remains the publisher. Bloomberg and LSEG are distribution routes the desk is
licensed to use, recorded as provenance and never as the source of the statistic.
Nothing here scrapes a news article, a press release or a vendor website, and no
balance is ever reconstructed from the text of a story.

## Current status

`READY_WITH_ENVIRONMENT_GATE`

Architecture, persistence contract, both provider adapters, point-in-time
machinery and the offline test suite are complete and pass on a machine that has
never seen a Bloomberg Terminal or an LSEG Workspace. The remaining gate is
corporate: entitlement, identifier discovery and a live smoke run. See
`VENDOR_INTEGRATION.md`, and the BRC repository's README for the shared
[status vocabulary](https://github.com/lucasweber1202/collector_brc_uk#status-vocabulary),
restated in `METHODOLOGY.md`.

**No live vendor query has been executed.** `LIVE_VENDOR_SMOKE = SKIP`, reason:
no corporate terminal or session available.

## Scope

CBI runs a large survey programme. Collecting it indiscriminately would bury the
handful of balances that bear on UK inflation, so the scope is the price and cost
side of three surveys plus the demand context needed to read it — 27 canonical
series.

**Four dimensions are never collapsed**, because collapsing any of them merges
different questions asked of different populations:

| Dimension | Values | Why it stays separate |
| --- | --- | --- |
| `survey` | `distributive_trades`, `industrial_trends`, `service_sector` | Three surveys, three samples, three questionnaires, three calendars. "CBI selling prices" is never one series. |
| `sector` | `total`, `business_professional`, `consumer` | The Service Sector Survey reports two distinct populations. |
| `measure` | `current`, `expected` | A forward-looking balance is a different economic object from a realised one. Merging them would bake a look-ahead into the series definition. |
| `stance` | `balance`, `level` | A net percentage balance is not a level. |

### Priority A — Distributive Trades Survey (monthly)

Maximum priority: `SELLING_PRICES_CURRENT`, `SELLING_PRICES_EXPECTED`.
Also collected as demand context: retail sales (current/expected), orders placed
upon suppliers (current/expected), stock adequacy.

### Priority B — Industrial Trends Survey (monthly)

Maximum priority: `SELLING_PRICES_CURRENT`, `SELLING_PRICES_EXPECTED`.
Also: average unit costs (current/expected), total orders, export orders, output
(current/expected). Capacity utilisation is deliberately absent — it is a
capacity measure, not a price measure, and no inflation justification for it was
established.

### Priority C — Service Sector Survey (quarterly)

For each of business/professional services and consumer services: average selling
prices (current/expected), average costs per person employed (current/expected),
business situation, expected employment.

Implementation is not forced here. This survey's vendor history is the least
certain of the three, and a series whose provider history turns out to be
discontinuous stays pending rather than being stitched.

The CBI Growth Indicator is a secondary priority and is not yet in the catalog.

## Install and test

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install ".[dev]"

pytest -q
ruff check .
ruff format --check .
mypy
python -m compileall -q .
```

All of that passes with no vendor library installed. Vendor libraries are
optional extras, because neither installs usefully without an entitled machine:

```powershell
python -m pip install ".[bloomberg]"   # blpapi, needs a running Terminal
python -m pip install ".[lseg]"        # lseg-data, needs a running Workspace
python -m pip install ".[vendors]"     # both
```

## Run

```powershell
copy .env.example .env      # then set COLLECTOR_DB_URL and DATA_PROVIDER
python main.py
```

Discovery, for the corporate machine:

```powershell
python -m scripts.discover_series --provider bloomberg --query "CBI selling prices"
python -m scripts.discover_series --provider lseg --query "CBI distributive trades selling prices"
```

## Provider roles

| Role | Setting | Behaviour |
| --- | --- | --- |
| primary | `DATA_PROVIDER` | Supplies the canonical history. Every stored value comes from here. |
| fallback | `FALLBACK_PROVIDER` | Used **only** when the primary is unreachable. Never on an entitlement failure or an unknown identifier, because both mean the configuration is wrong. |
| cross-check | `CROSS_CHECK_PROVIDER` | Read and compared, never written. A disagreement past `CROSS_CHECK_TOLERANCE` (default 0.5 balance points) fails the run. |

The database never gains a second economic series because the vendor changed.

## Licence and safety

CBI survey data is licensed. This repository contains no CBI values, no
historical fixtures derived from a vendor, no credentials and no vendor
responses. Every test fixture is synthetic and structurally equivalent. Raw
vendor payloads go to a gitignored `_raw/` directory and are never committed.
Redistribution is out of scope: data is stored for internal research under the
desk's own entitlement.

## Documents

- `METHODOLOGY.md` — what is collected, how it is validated, how the providers converge.
- `POINT_IN_TIME.md` — the point-in-time contract, and why a vendor backfill is `first_seen`.
- `VENDOR_INTEGRATION.md` — the corporate-machine runbook: entitlement, discovery, smoke.
