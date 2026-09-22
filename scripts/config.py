"""Runtime settings for collector_cbi_uk.

The schema name equals the repository name, as the fleet requires. Vendor
settings are deliberately thin: the Bloomberg Desktop API authenticates through
the logged-in Terminal on the local machine and takes no secret, and the LSEG
Data Library reads its own configuration file. This module therefore exposes
which provider to use and where to reach it, and never a credential.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = ROOT_DIR / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value and key not in os.environ:
            os.environ[key] = value

SCHEMA_NAME = "collector_cbi_uk"
CATALOG_NAME = "macrobond_inhouse"
METADATA_TABLE = "metadata"
TIME_SERIES_TABLE = "time_series"
AVAILABILITY_TABLE = "availability"
SNAPSHOTS_TABLE = "source_snapshots"
VENDOR_PROVENANCE_TABLE = "vendor_provenance"
LOGS_TABLE = "logs"
COUNTRY_CURRENCY = "GBP"

SOURCE_ID = "cbi_economic_surveys"
ORIGINAL_PUBLISHER = "Confederation of British Industry"
PUBLISHER_URL = "https://www.cbi.org.uk/economics/surveys/"
PUBLICATION_TIMEZONE = "Europe/London"

BLOOMBERG = "bloomberg"
LSEG = "lseg"
PROVIDERS = (BLOOMBERG, LSEG)

PROD = os.getenv("PROD", "false").lower() in ("1", "true", "yes")
DATABASE_URL = os.getenv("COLLECTOR_DB_URL", "")
RAW_DIR = Path(os.getenv("COLLECTOR_RAW_DIR", str(ROOT_DIR / "_raw")))
if not RAW_DIR.is_absolute():
    RAW_DIR = ROOT_DIR / RAW_DIR
VENDOR_SERIES_FILE = Path(
    os.getenv("COLLECTOR_VENDOR_SERIES_FILE", str(ROOT_DIR / "config" / "vendor_series.csv"))
)
LOG_LEVEL = os.getenv("COLLECTOR_LOG_LEVEL", "INFO")

# Which licensed delivery provider supplies the canonical history, and which
# one (if any) is consulted afterwards. A cross-check provider is read and
# compared but never written: the canonical value always comes from the primary
# so a provider outage cannot silently change the economic history.
DATA_PROVIDER = os.getenv("DATA_PROVIDER", BLOOMBERG).strip().lower()
FALLBACK_PROVIDER = os.getenv("FALLBACK_PROVIDER", "").strip().lower()
CROSS_CHECK_PROVIDER = os.getenv("CROSS_CHECK_PROVIDER", "").strip().lower()
CROSS_CHECK_TOLERANCE = float(os.getenv("CROSS_CHECK_TOLERANCE", "0.5"))

# Bloomberg Desktop API. The Terminal serves it on the local machine; the
# defaults below are the Desktop API defaults and are overridable only because
# some desks run the service on a different port.
BLOOMBERG_HOST = os.getenv("BLOOMBERG_HOST", "localhost")
BLOOMBERG_PORT = int(os.getenv("BLOOMBERG_PORT", "8194"))

# LSEG Data Library. The Desktop session authenticates through a running
# Workspace; the library reads its own lseg-data.config.json, whose location is
# LD_LIB_CONFIG_PATH. Nothing here holds a key.
LSEG_SESSION = os.getenv("LSEG_SESSION", "desktop.workspace")

HISTORY_START = os.getenv("COLLECTOR_HISTORY_START", "1990-01-01")
DBX_SERVER_HOSTNAME = os.getenv("DBX_SERVER_HOSTNAME", "")
DBX_HTTP_PATH = os.getenv("DBX_HTTP_PATH", "")
AKV_VAULT_URL = os.getenv("AKV_VAULT_URL", "")
AKV_SECRET_NAME = os.getenv("AKV_SECRET_NAME", "databricks-token")


def missing_environment(prod: bool = PROD) -> list[str]:
    """Return required environment variables that are not set."""
    missing: list[str] = []
    if prod:
        required = {"DBX_SERVER_HOSTNAME": DBX_SERVER_HOSTNAME, "DBX_HTTP_PATH": DBX_HTTP_PATH}
        missing.extend(sorted(name for name, value in required.items() if not value))
    elif not DATABASE_URL:
        missing.append("COLLECTOR_DB_URL")
    if DATA_PROVIDER not in PROVIDERS:
        missing.append("DATA_PROVIDER")
    return missing


def unresolved_credentials(prod: bool = PROD) -> list[str]:
    """Return credentials that must resolve from the runtime context."""
    if prod and not os.getenv("DATABRICKS_TOKEN") and not AKV_VAULT_URL:
        return ["DATABRICKS_TOKEN", "AKV_VAULT_URL"]
    return []
