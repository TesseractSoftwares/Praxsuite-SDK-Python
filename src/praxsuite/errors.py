"""Every failure the gateway reports arrives as a ``PraxError``.

Exceptions are idiomatic in Python, so unlike the Godot SDK these are raised rather than
returned. Subclasses exist for the failures worth branching on, so ``except`` reads naturally::

    try:
        page = prax.data.table("Scores").fetch()
    except PraxRateLimitError:
        ...          # transient - back off and retry
    except PraxQuotaExceededError:
        ...          # NOT transient - the workspace owner has to upgrade

``code`` is stable and safe to branch on. ``message`` is human-facing and may change.
"""

from __future__ import annotations

from typing import Sequence

__all__ = [
    "PraxError",
    "PraxAuthError",
    "PraxForbiddenError",
    "PraxRateLimitError",
    "PraxQuotaExceededError",
    "PraxNetworkError",
    "PraxTimeoutError",
    "PraxValidationError",
]


class PraxError(Exception):
    """Base class for everything this SDK raises."""

    def __init__(
        self,
        code: str = "UNKNOWN",
        message: str = "",
        status: int = 0,
        details: Sequence[str] = (),
        raw_body: str = "",
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        #: HTTP status, or 0 for a transport failure that never reached the gateway.
        self.status = status
        #: Per-field validation details, when the gateway supplied them.
        self.details = tuple(details)
        #: Raw response body, kept for diagnostics. Never contains your API key.
        self.raw_body = raw_body

    @property
    def is_auth_failure(self) -> bool:
        """The credential is missing, malformed, expired, or the session needs a refresh."""
        return self.status == 401

    @property
    def is_forbidden(self) -> bool:
        """Authenticated, but this credential or role is not scoped for the operation."""
        return self.status == 403

    @property
    def is_rate_limited(self) -> bool:
        """Too many calls per minute. Backing off and retrying will succeed."""
        return self.code == "RATE_LIMIT_EXCEEDED"

    @property
    def is_quota_exceeded(self) -> bool:
        """A plan allowance is exhausted. Retrying will NOT help.

        Shares HTTP 429 with a rate limit, which is exactly why this is a separate check.
        """
        return self.code in ("QUOTA_EXCEEDED", "EGRESS_LIMIT_EXCEEDED")

    @property
    def is_network_error(self) -> bool:
        """Transport failure: offline, DNS, TLS, or timeout."""
        return self.code in ("NETWORK_ERROR", "TIMEOUT")

    @property
    def is_transient(self) -> bool:
        """Worth retrying automatically. Quota exhaustion deliberately is not."""
        if self.is_quota_exceeded:
            return False
        return self.is_network_error or self.is_rate_limited or 500 <= self.status <= 599

    def __str__(self) -> str:
        parts = [f"[Praxsuite] {self.code}"]
        if self.status:
            parts.append(f" (HTTP {self.status})")
        parts.append(f": {self.message}")
        if self.details:
            parts.append("\n  - " + "\n  - ".join(self.details))
        return "".join(parts)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, status={self.status!r})"


class PraxAuthError(PraxError):
    """HTTP 401. Not signed in, or the credential does not belong to this workspace."""


class PraxForbiddenError(PraxError):
    """HTTP 403. Signed in, but not scoped for this table or operation."""


class PraxRateLimitError(PraxError):
    """Too many requests. Transient - retry after a backoff."""


class PraxQuotaExceededError(PraxError):
    """A plan allowance is exhausted. Not transient; retrying will not help."""


class PraxNetworkError(PraxError):
    """The request never reached the gateway."""


class PraxTimeoutError(PraxNetworkError):
    """The gateway did not answer in time."""


class PraxValidationError(PraxError, ValueError):
    """The SDK refused the request before sending it.

    Also a ``ValueError``, because from a caller's point of view that is what a bad argument is,
    and code already catching ValueError should not have to learn a new type.
    """


def error_for(
    code: str, message: str, status: int = 0, details: Sequence[str] = (), raw_body: str = ""
) -> PraxError:
    """Builds the most specific error class for a code/status pair.

    Classification lives here rather than at each call site so the sync client, the async client
    and the tests cannot disagree about what a 429 means.
    """
    if code in ("QUOTA_EXCEEDED", "EGRESS_LIMIT_EXCEEDED"):
        cls: type[PraxError] = PraxQuotaExceededError
    elif code == "RATE_LIMIT_EXCEEDED":
        cls = PraxRateLimitError
    elif code == "TIMEOUT":
        cls = PraxTimeoutError
    elif code == "NETWORK_ERROR":
        cls = PraxNetworkError
    elif status == 401:
        cls = PraxAuthError
    elif status == 403:
        cls = PraxForbiddenError
    else:
        cls = PraxError
    return cls(code, message, status, details, raw_body)
