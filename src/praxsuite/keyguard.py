"""Tells a publishable credential from a secret one, and refuses the wrong one.

A Python process is usually a server you control, so unlike the game-engine SDKs a secret key is
often exactly the right thing to use here. What this module prevents is the opposite mistake:
shipping a secret key into something that is not a server - a notebook published to a repo, a
PyScript or Pyodide page, a desktop app, a Jupyter kernel someone else can reach.

That distinction is a decision the caller makes, so ``check_client_safe`` is used where the SDK
knows the credential will be exposed, and ``classify`` is available everywhere else.
"""

from __future__ import annotations

import enum
import re

__all__ = ["CredentialKind", "classify", "check_client_safe", "redact", "is_secret"]

PUBLISHABLE_PREFIX = "pk_live_"
SECRET_PREFIX = "sk_live_"

_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class CredentialKind(enum.Enum):
    UNKNOWN = "unknown"
    PUBLISHABLE = "publishable"
    SECRET = "secret"
    JWT = "jwt"


def classify(credential: str | None) -> CredentialKind:
    """Identifies a credential by shape alone. Never contacts the network."""
    if not credential:
        return CredentialKind.UNKNOWN
    value = credential.strip()
    if value.startswith(SECRET_PREFIX):
        return CredentialKind.SECRET
    if value.startswith(PUBLISHABLE_PREFIX):
        return CredentialKind.PUBLISHABLE
    if _JWT_RE.match(value):
        return CredentialKind.JWT
    return CredentialKind.UNKNOWN


def is_secret(credential: str | None) -> bool:
    return classify(credential) is CredentialKind.SECRET


def check_client_safe(credential: str, context: str) -> None:
    """Raises when a secret key is about to be used somewhere it would be exposed.

    Raises:
        PraxValidationError: if ``credential`` is an ``sk_live_`` key.
    """
    from .errors import PraxValidationError

    if is_secret(credential):
        raise PraxValidationError(
            "SECRET_KEY_REFUSED",
            f"{context} was given a secret key (sk_live_...). Anything that reaches a browser, "
            f"a notebook you publish, or a user's machine must use a publishable key "
            f"(pk_live_...) instead. Revoke this key if it has already been distributed.",
        )


def redact(credential: str | None) -> str:
    """Masks a credential for display, keeping only enough to identify which one it was."""
    if not credential:
        return "<empty>"
    value = credential.strip()
    for prefix in (SECRET_PREFIX, PUBLISHABLE_PREFIX):
        if value.startswith(prefix):
            tail = value[len(prefix):]
            return prefix + (tail[:4] + "..." if len(tail) > 4 else "...")
    if _JWT_RE.match(value):
        return "<jwt>"
    return value[:2] + "..." if len(value) > 2 else "..."
