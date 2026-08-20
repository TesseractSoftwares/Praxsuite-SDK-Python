"""User accounts: register, sign in, refresh, sign out, password reset.

Reached through the client: ``prax.auth``.

Auth routes skip table-scope checks, so register/login/refresh work on a credential with no table
scopes at all. That is the credential a client-facing application should carry.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping

from . import routes
from .errors import PraxAuthError, PraxError, PraxValidationError
from .log import logger
from .result import unwrap_envelope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Praxsuite

__all__ = ["PraxAuth", "Session", "RegistrationResult"]

#: Refresh this many seconds before the access token actually expires, so it happens between
#: requests rather than in the middle of one. Covers a slow round trip.
REFRESH_SKEW_SECONDS = 60


@dataclass
class Session:
    """A signed-in user's session."""

    access_token: str = ""
    refresh_token: str = ""

    #: When the access token stops being accepted. Unix seconds, UTC.
    expires_at: float = 0.0

    user_id: str = ""
    email: str = ""
    display_name: str = ""

    #: Any additional profile fields the workspace returns, kept verbatim so a refresh can carry
    #: them forward.
    profile: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return bool(self.access_token)

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at - REFRESH_SKEW_SECONDS

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any],
                     previous: "Session | None" = None) -> "Session":
        session = cls(
            access_token=str(payload.get("accessToken") or ""),
            refresh_token=str(payload.get("refreshToken") or ""),
        )

        # The gateway reports a lifetime in seconds; an absolute instant is what callers need.
        expires_in = payload.get("expiresIn")
        if expires_in:
            try:
                session.expires_at = time.time() + float(expires_in)
            except (TypeError, ValueError):
                pass

        user = payload.get("user")
        if isinstance(user, dict):
            session.user_id = str(user.get("id") or user.get("userId") or "")
            session.email = str(user.get("email") or "")
            session.display_name = str(user.get("displayName") or user.get("name") or "")
            session.profile = dict(user)

        # A refresh carries tokens but not always the user block. Carry the old identity forward
        # rather than presenting a signed-in user as anonymous.
        if previous is not None:
            session.user_id = session.user_id or previous.user_id
            session.email = session.email or previous.email
            session.display_name = session.display_name or previous.display_name
            session.profile = session.profile or dict(previous.profile)
            session.refresh_token = session.refresh_token or previous.refresh_token

        return session


@dataclass
class RegistrationResult:
    """Register succeeded, but whether a session came with it depends on the workspace."""

    #: Set when the workspace requires email confirmation. There is NO session in that case, and
    #: this is not a failure - telling the user their password was wrong would leave them
    #: retrying a correct one forever.
    requires_email_confirmation: bool = False
    session: Session | None = None
    message: str = ""


class PraxAuth:
    """Session handling. Thread-safe: a refresh is serialised across threads."""

    def __init__(self, client: "Praxsuite") -> None:
        self._client = client
        self._session: Session | None = None
        # The gateway retires the old refresh token as it issues the new one, so two concurrent
        # refreshes would leave the loser holding a token the server has already invalidated.
        # One lock, and the second caller re-checks whether the first already succeeded.
        self._refresh_lock = threading.Lock()
        self._on_session_change: list[Callable[[Session | None], None]] = []

    @property
    def session(self) -> Session | None:
        return self._session

    @property
    def is_signed_in(self) -> bool:
        return self._session is not None and self._session.is_valid

    def on_session_change(self, callback: Callable[[Session | None], None]) -> None:
        """Registers a callback fired whenever the signed-in user changes, sign-out included.

        Useful for clearing per-user caches, or for sending someone back to a login screen when a
        refresh fails, rather than discovering it on their next query.
        """
        self._on_session_change.append(callback)

    # ── sign in and out ─────────────────────────────────────────────────────

    def register(self, email: str, password: str, **extra_fields: Any) -> RegistrationResult:
        """Creates an account.

        Check ``requires_email_confirmation`` on the result before assuming a session exists.
        """
        body = {"email": email, "password": password, **extra_fields}
        payload = self._post("register", body)

        result = RegistrationResult(
            message=str(payload.get("message") or ""),
            requires_email_confirmation=bool(payload.get("requiresEmailConfirmation")),
        )
        if not result.requires_email_confirmation and payload.get("accessToken"):
            result.session = self._adopt(Session.from_payload(payload))
        return result

    def login(self, email: str, password: str) -> Session:
        payload = self._post("login", {"email": email, "password": password})
        return self._adopt(Session.from_payload(payload))

    def logout(self) -> None:
        """Signs out and clears the session.

        The local session is cleared even if the server call fails - someone who pressed sign out
        must end up signed out.
        """
        session = self._session
        try:
            if session and session.refresh_token:
                self._post("logout", {"refreshToken": session.refresh_token})
        except PraxError as exc:
            logger.debug("Server-side logout failed, clearing the local session anyway: %s", exc)
        finally:
            self._set_session(None)

    # ── session maintenance ─────────────────────────────────────────────────

    def ensure_fresh_session(self) -> None:
        """Refreshes if the access token is close to expiry.

        Called automatically before every request, so you should not normally need it.
        """
        session = self._session
        if session is None or not session.is_valid or not session.needs_refresh:
            return

        with self._refresh_lock:
            current = self._session
            # Another thread may have refreshed while this one waited for the lock.
            if current is None or not current.is_valid or not current.needs_refresh:
                return
            self._refresh_locked(current)

    def refresh(self) -> Session:
        """Forces a refresh now."""
        with self._refresh_lock:
            current = self._session
            if current is None or not current.is_valid:
                raise PraxAuthError("NOT_SIGNED_IN", "There is no session to refresh.", 401)
            return self._refresh_locked(current)

    def _refresh_locked(self, current: Session) -> Session:
        if not current.refresh_token:
            self._set_session(None)
            raise PraxAuthError(
                "SESSION_EXPIRED",
                "The session expired and there is no refresh token, so the user has been "
                "signed out.",
                401,
            )
        try:
            payload = self._post("refresh", {"refreshToken": current.refresh_token})
        except PraxError as exc:
            # A rejected refresh token is final. A network blip is not - keep the session, since
            # the existing token may still work.
            if exc.is_auth_failure:
                self._set_session(None)
            raise
        return self._adopt(Session.from_payload(payload, current))

    # ── password reset and confirmation ─────────────────────────────────────
    #
    # These always report success, whether or not the address exists. That is deliberate on the
    # server's part: it stops the endpoint being used to discover which addresses have accounts.
    # Do not "helpfully" report that no such account exists - that reintroduces the leak.

    def forgot_password(self, email: str) -> None:
        self._post("forgot-password", {"email": email})

    def verify_reset_code(self, email: str, code: str) -> None:
        self._post("verify-reset-code", {"email": email, "code": code})

    def reset_password(self, email: str, code: str, new_password: str) -> None:
        self._post("reset-password",
                   {"email": email, "code": code, "newPassword": new_password})

    def resend_confirmation(self, email: str) -> None:
        self._post("resend-confirmation", {"email": email})

    # ── config ──────────────────────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        """Reads the workspace's public auth configuration.

        This route is UNAUTHENTICATED. A workspace id alone is enough to fetch it, which is why a
        workspace id is not a secret - but also why it does not belong in a published example.
        """
        url = routes.auth(self._client.base_url, self._client.workspace_id, "config")
        body = self._client.transport.request_json(
            "GET", url, self._client.anonymous_headers(), None, retry_safe=True
        )
        return unwrap_envelope(body)

    # ── plumbing ────────────────────────────────────────────────────────────

    def _adopt(self, session: Session) -> Session:
        if not session.is_valid:
            raise PraxValidationError(
                "MALFORMED_RESPONSE", "The gateway returned no access token."
            )
        self._set_session(session)
        return session

    def _set_session(self, session: Session | None) -> None:
        self._session = session
        for callback in list(self._on_session_change):
            try:
                callback(session)
            except Exception:
                # A caller's callback must not break sign-in.
                logger.exception("A session-change callback raised.")

    def _post(self, action: str, body: Mapping[str, Any]) -> dict[str, Any]:
        url = routes.auth(self._client.base_url, self._client.workspace_id, action)
        # Auth calls carry the credential, never the session: signing in while already signed in
        # must not depend on the old token still being valid.
        response = self._client.transport.request_json(
            "POST", url, self._client.anonymous_headers(), dict(body), retry_safe=False
        )
        return unwrap_envelope(response)
