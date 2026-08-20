"""Calls a workspace's custom gateway endpoints.

An endpoint runs an automation on the server, which is where anything a caller must not be able
to influence belongs: awarding credit, validating a submission, granting access. The client asks
for an outcome; the server decides it.

::

    result = prax.endpoints.call("submit-score", {"score": score})
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from . import routes
from .errors import PraxValidationError
from .result import unwrap_envelope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Praxsuite

__all__ = ["PraxEndpoints"]


class PraxEndpoints:
    def __init__(self, client: "Praxsuite") -> None:
        self._client = client

    def call(self, slug: str, body: Any = None) -> dict[str, Any]:
        """POSTs to an endpoint and returns its response body.

        Not retried automatically: an endpoint runs an automation, and running one twice is rarely
        harmless. Retry deliberately if you know the endpoint is idempotent.
        """
        url = self._url(slug)
        response = self._client.send("POST", url, body, retry_safe=False)
        # An endpoint's response is whatever its automation returns, so it may or may not be
        # platform-enveloped. unwrap_envelope leaves a bare body alone.
        return unwrap_envelope(response)

    def get(self, slug: str) -> dict[str, Any]:
        """GETs an endpoint. Safe to retry, so transient failures are retried automatically."""
        response = self._client.send("GET", self._url(slug), None, retry_safe=True)
        return unwrap_envelope(response)

    def _url(self, slug: str) -> str:
        if not slug or not slug.strip():
            raise PraxValidationError("INVALID_REQUEST", "An endpoint slug is required.")
        return routes.endpoint(self._client.base_url, self._client.workspace_id, slug.strip())
