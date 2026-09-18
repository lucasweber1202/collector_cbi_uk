# Data audit — collector_cbi_uk

- Audit seed: `20260918`
- Tests: **89 passed**
- Execution: **BLOCKED**
- Values: **NOT_VERIFIABLE**
- Metadata: **NOT_VERIFIABLE**
- Filtering: **PARTIAL** (canonical catalog and adapter validation only)
- PIT: **PARTIAL** (fixture-tested; no vendor observations)
- Latest: **BLOCKED**
- Overall: **BLOCKED**

## Evidence

The canonical catalog defines 27 CBI surveys series, but every Bloomberg and LSEG registry row still contains `PENDING_VENDOR_DISCOVERY`. A live call stops before requesting data. No ticker, RIC, mnemonic, field, value, release timestamp or history start has been guessed.

The adapter suites validate request construction, provider errors, monthly date normalization, Europe/London timezone conversion, observed-value versus release-metadata separation, snapshot identity, idempotency and vintage behavior. These tests do not prove that a real vendor identifier represents the intended economic series.

| Sample | Series | Period | Collector | Official source | Metadata | Filter | PIT | Result |
|---|---|---|---|---|---|---|---|---|
| 1–27 | canonical catalog | — | — | Bloomberg/LSEG unavailable | NOT_VERIFIABLE | adapter only | fixture only | **NOT_VERIFIABLE** |

## Required external evidence

Run `python -m scripts.discover_series --provider bloomberg` and/or the LSEG equivalent on an entitled corporate machine. For every retained series, record and independently verify the identifier, observed-value field, description, unit, periodicity, history start and publication/release field. `PX_LAST` or an equivalent observed-value field must not be replaced by a survey median or forecast field.
