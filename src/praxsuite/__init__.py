"""Praxsuite SDK for Python.

Auth, queries, files and server-authoritative endpoints. Zero dependencies - everything here is
standard library, so ``pip install praxsuite`` adds nothing to your lock file.

::

    from praxsuite import Praxsuite, filters as f

    prax = Praxsuite("your-workspace-id", "sk_live_...")

    page = (prax.data.table("Orders")
            .select("ID", "Total", "Status")
            .where(f.gte("Total", 100), Status="paid")
            .order_by("Total", ascending=False)
            .limit(50)
            .fetch())

    for row in page:
        print(row["ID"], row["Total"])

For async code (FastAPI and friends) see :mod:`praxsuite.aio`.
"""

from . import filters
from .auth import PraxAuth, RegistrationResult, Session
from .client import SDK_VERSION, Praxsuite
from .data import NATIVE_COLUMNS, PraxData, Query
from .endpoints import PraxEndpoints
from .errors import (
    PraxAuthError,
    PraxError,
    PraxForbiddenError,
    PraxNetworkError,
    PraxQuotaExceededError,
    PraxRateLimitError,
    PraxTimeoutError,
    PraxValidationError,
)
from .keyguard import CredentialKind, classify, redact
from .log import logger, scrub
from .result import MutationResult, Page
from .routes import CLOUD_HOST
from .schema import PraxSchema

__version__ = SDK_VERSION

#: Alias, for anyone who prefers the longer name.
PraxsuiteClient = Praxsuite

__all__ = [
    "Praxsuite",
    "PraxsuiteClient",
    "filters",
    # results
    "Page",
    "MutationResult",
    "Session",
    "RegistrationResult",
    # components
    "PraxAuth",
    "PraxData",
    "PraxSchema",
    "PraxEndpoints",
    "Query",
    # errors
    "PraxError",
    "PraxAuthError",
    "PraxForbiddenError",
    "PraxRateLimitError",
    "PraxQuotaExceededError",
    "PraxNetworkError",
    "PraxTimeoutError",
    "PraxValidationError",
    # helpers
    "CredentialKind",
    "classify",
    "redact",
    "scrub",
    "logger",
    "CLOUD_HOST",
    "NATIVE_COLUMNS",
    "SDK_VERSION",
    "__version__",
]
