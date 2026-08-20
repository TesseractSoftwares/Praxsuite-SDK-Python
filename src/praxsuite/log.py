"""SDK logging, on the standard ``logging`` module, with credentials scrubbed.

Python already has a logging framework and an SDK that invents its own is a nuisance, so this is
a thin layer: a ``praxsuite`` logger plus a filter that removes credentials from any record
passing through it. Configure verbosity the normal way::

    logging.getLogger("praxsuite").setLevel(logging.DEBUG)

The scrubbing is attached to the logger rather than applied at each call site, so a message
logged by a caller's own code through this logger is cleaned too.
"""

from __future__ import annotations

import logging
import re

__all__ = ["logger", "scrub", "SecretScrubbingFilter"]

# Keeps the prefix so a log still says WHICH kind of key was involved, and drops the material.
_KEY_RE = re.compile(r"\b(pk_live_|sk_live_)[A-Za-z0-9]{4,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
_FIELD_RE = re.compile(
    r'"(refreshToken|accessToken|password|newPassword|currentPassword|confirmPassword'
    r'|sessionToken|publicKey)"\s*:\s*"[^"]*"'
)


def scrub(text: str) -> str:
    """Removes credentials from a string.

    Public because callers building their own diagnostics should run untrusted text through it
    too - a gateway response body pasted into a bug report is the usual culprit.
    """
    if not text:
        return text
    out = _KEY_RE.sub(lambda m: m.group(1) + "<redacted>", text)
    out = _JWT_RE.sub("<jwt redacted>", out)
    out = _FIELD_RE.sub(lambda m: f'"{m.group(1)}":"<redacted>"', out)
    return out


class SecretScrubbingFilter(logging.Filter):
    """Scrubs credentials out of every record passing through the logger it is attached to."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Formatting now rather than leaving args for the handler: a credential passed as a
        # lazy %s argument would otherwise reach the handler unscrubbed.
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = scrub(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


logger = logging.getLogger("praxsuite")
logger.addFilter(SecretScrubbingFilter())

# A library must not configure the root logger, and must not warn when an application has not
# set up logging at all.
logger.addHandler(logging.NullHandler())
