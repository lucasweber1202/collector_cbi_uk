"""Explicit, auditable failure states for a licensed delivery provider.

The single most dangerous failure mode for a vendor-delivered collector is a
silent empty result. Bloomberg and LSEG both answer an unentitled request, an
unknown identifier and a genuinely empty window with something that looks like
"no rows", and a collector that treats that as success writes nothing, logs
success, and quietly stops tracking a series forever.

Every state below is therefore a distinct exception. Nothing in this repository
converts an empty vendor response into an empty success.
"""

from __future__ import annotations


class VendorError(RuntimeError):
    """Base class for every licensed-provider failure."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class ProviderUnavailableError(VendorError):
    """The provider library or its local session could not be reached.

    Bloomberg: blpapi is not installed, or the Terminal is not running so the
    Desktop API service on localhost:8194 refuses the session.
    LSEG: the lseg.data library is not installed, or Workspace is not running.
    """


class VendorAuthenticationError(VendorError):
    """The provider was reachable but rejected the session's identity."""


class EntitlementError(VendorError):
    """The session is valid but not entitled to this data.

    This is never a reason to fall back to another series or to return nothing.
    The desk has to be entitled, and the run must fail loudly until it is.
    """


class IdentifierNotFoundError(VendorError):
    """The vendor does not recognise the requested instrument identifier."""


class FieldNotFoundError(VendorError):
    """The instrument exists but the vendor does not recognise the field."""


class EmptyHistoryError(VendorError):
    """The request succeeded and returned no observations at all.

    Distinct from IdentifierNotFoundError on purpose: a valid, entitled series
    that returns nothing is still a failure for this collector, because the
    series is known to be published and an empty history means the query, the
    window or the vendor's own state is wrong.
    """


class TemporaryProviderError(VendorError):
    """A transient provider-side condition: timeout, throttle, service restart.

    Separated from the permanent failures so an operator can retry this one and
    only this one without re-examining entitlements.
    """


class PendingVendorDiscoveryError(VendorError):
    """The series has no confirmed vendor identifier yet.

    Raised instead of guessing. The identifier is discovered inside the
    corporate environment (see scripts/discover_series.py) and written into
    config/vendor_series.csv; nothing else in the collector has to change.
    """

    def __init__(self, provider: str, series_id: str) -> None:
        super().__init__(
            provider,
            f"{series_id} has no confirmed vendor identifier. Run "
            f"'python -m scripts.discover_series --provider {provider}' on an "
            "entitled machine and record the result in config/vendor_series.csv.",
        )
