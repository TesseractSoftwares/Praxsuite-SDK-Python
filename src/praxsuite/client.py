"""The Praxsuite client.

::

    from praxsuite import Praxsuite

    prax = Praxsuite("your-workspace-id", "sk_live_...")     # server-side
    page = prax.data.table("Orders").where(Status="paid").limit(50).fetch()

Which key to use depends on where this code runs:

* **A server you control** - a secret key (``sk_live_``) is correct. It is the whole point of one.
* **Anything a user can read** - a published notebook, a Pyodide or PyScript page, a desktop app,
  a shared Jupyter kernel - a publishable key (``pk_live_``), and pass ``client_side=True`` so the
  SDK refuses a secret key outright rather than trusting you to remember.

Be aware that every credential carries BOTH halves; there is no publishable-only credential. So
whatever tables you scope to a credential are reachable by anyone holding the workspace id, since
``/{workspace}/auth/config`` is unauthenticated. Scope narrowly.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from . import keyguard, routes
from .auth import PraxAuth
from .data import PraxData, Query
from .endpoints import PraxEndpoints
from .errors import PraxError, PraxValidationError
from .http import HttpTransport
from .log import logger
from .schema import PraxSchema

__all__ = ["Praxsuite"]

SDK_VERSION = "1.0.0"


class Praxsuite:
    """A configured connection to one Praxsuite workspace.

    Cheap to construct and safe to share between threads. Sessions and the schema cache live on
    the instance, so one instance per workspace per process is the usual arrangement.
    """

    def __init__(
        self,
        workspace_id: str | None = None,
        credential: str | None = None,
        base_url: str | None = None,
        *,
        client_side: bool = False,
        timeout: float = 20.0,
        max_attempts: int = 3,
    ) -> None:
        """
        Args:
            workspace_id: Falls back to ``PRAXSUITE_WORKSPACE_ID``.
            credential: An API key. Falls back to ``PRAXSUITE_API_KEY``.
            base_url: Gateway host. Falls back to ``PRAXSUITE_BASE_URL``, then the cloud gateway.
            client_side: Set when this code runs somewhere a user can read it. Makes a secret key
                a hard error instead of a judgement call.
            timeout: Per-request timeout in seconds.
            max_attempts: Attempts for idempotent requests. 1 disables retries.
        """
        workspace_id = workspace_id or os.environ.get("PRAXSUITE_WORKSPACE_ID") or ""
        credential = credential or os.environ.get("PRAXSUITE_API_KEY") or ""
        base_url = base_url or os.environ.get("PRAXSUITE_BASE_URL") or routes.CLOUD_HOST

        if not workspace_id.strip():
            raise PraxValidationError(
                "MISSING_WORKSPACE",
                "A workspace id is required. Pass it, or set PRAXSUITE_WORKSPACE_ID.",
            )
        if not credential.strip():
            raise PraxValidationError(
                "MISSING_CREDENTIAL",
                "An API key is required. Pass it, or set PRAXSUITE_API_KEY. Create one in your "
                "workspace under API Gateway.",
            )

        if client_side:
            keyguard.check_client_safe(credential.strip(), "Praxsuite(client_side=True)")

        self.workspace_id = workspace_id.strip()
        self.base_url = routes.normalize_base_url(base_url)
        self._credential = credential.strip()
        self._client_side = client_side

        if routes.is_insecure_remote(self.base_url):
            # Not fatal - a LAN test server is a legitimate target - but a plaintext connection
            # puts the credential on the wire for anyone on the network to read.
            logger.warning(
                "%s is plaintext HTTP. Credentials and session tokens will travel unencrypted; "
                "use https for anything but local testing.", self.base_url,
            )

        self.transport = HttpTransport(timeout=timeout, max_attempts=max_attempts)
        self.auth = PraxAuth(self)
        self.data = PraxData(self)
        self.schema = PraxSchema(self)
        self.endpoints = PraxEndpoints(self)

        logger.info(
            "Configured for workspace %s at %s using %s (SDK %s)",
            self.workspace_id, self.base_url, keyguard.redact(self._credential), SDK_VERSION,
        )

    # ── headers ─────────────────────────────────────────────────────────────

    def _base_headers(self) -> dict[str, str]:
        return {"x-praxsuite-sdk": f"python/{SDK_VERSION}"}

    def anonymous_headers(self) -> dict[str, str]:
        """Headers for a call that must NOT carry a user's session: sign-in, and auth/config."""
        headers = self._base_headers()
        headers["x-api-key"] = self._credential
        return headers

    def session_headers(self) -> dict[str, str]:
        """Headers for everything else.

        A signed-in user's session takes precedence, so row filters and role scopes apply to them
        rather than to the anonymous credential. The gateway accepts either header, never both:
        Authorization carries a session token, x-api-key carries a key.
        """
        headers = self._base_headers()
        session = self.auth.session
        if session is not None and session.is_valid:
            headers["Authorization"] = f"Bearer {session.access_token}"
        else:
            headers["x-api-key"] = self._credential
        return headers

    # ── requests ────────────────────────────────────────────────────────────

    def send(
        self,
        method: str,
        url: str,
        body: Any = None,
        *,
        retry_safe: bool = False,
        _already_refreshed: bool = False,
    ) -> dict[str, Any]:
        """Sends an authorised request, refreshing the session first if it is near expiry.

        A 401 on a signed-in request is retried once after a refresh: an access token can expire
        between the check and the server reading it.
        """
        if self.auth.is_signed_in:
            try:
                self.auth.ensure_fresh_session()
            except PraxError as exc:
                # Only fatal if it actually signed the user out. A network blip leaves the old
                # token in place, and it may still work.
                if not self.auth.is_signed_in:
                    raise
                logger.debug("Pre-flight refresh failed but the session survives: %s", exc)

        try:
            return self.transport.request_json(
                method, url, self.session_headers(), body, retry_safe=retry_safe
            )
        except PraxError as exc:
            if (
                exc.is_auth_failure
                and self.auth.is_signed_in
                and not _already_refreshed
            ):
                try:
                    self.auth.refresh()
                except PraxError:
                    raise exc from None
                return self.send(
                    method, url, body, retry_safe=retry_safe, _already_refreshed=True
                )
            raise

    # ── convenience ─────────────────────────────────────────────────────────

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Sends a hand-built PraxQL request. Shorthand for ``prax.data.execute``."""
        return self.data.execute(request)

    def table(self, name_or_id: str) -> Query:
        """Shorthand for ``prax.data.table``."""
        return self.data.table(name_or_id)

    def __repr__(self) -> str:
        return (
            f"Praxsuite(workspace_id={self.workspace_id!r}, base_url={self.base_url!r}, "
            f"credential={keyguard.redact(self._credential)!r})"
        )
