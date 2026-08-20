"""Builds gateway URLs.

The Praxsuite FrontDoor accepts a short form, ``/{workspace_id}/query``, which it rewrites to the
backend's ``/api/v1/gateway/{workspace_id}/query``. The SDK uses the short form: it is the
documented public shape, and going through the FrontDoor is what applies the edge rate limit.

Host matters. Praxsuite runs several independent tiers and a workspace exists on exactly one - a
workspace on another tier returns 404, not an error you can diagnose from the message.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["CLOUD_HOST", "normalize_base_url", "is_insecure_remote", "query", "schema", "auth",
           "endpoint", "files"]

CLOUD_HOST = "https://gateway.praxsuite.com"

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def normalize_base_url(base_url: str | None) -> str:
    """Trims trailing slashes and defaults to https."""
    if not base_url or not base_url.strip():
        return CLOUD_HOST
    url = base_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def is_insecure_remote(base_url: str) -> bool:
    """True for a plaintext URL that is not a loopback address."""
    if not base_url.lower().startswith("http://"):
        return False
    host = base_url[7:].split("/", 1)[0].split(":", 1)[0].lower()
    return host not in _LOOPBACK


def _workspace_base(base_url: str, workspace_id: str) -> str:
    return f"{normalize_base_url(base_url)}/{workspace_id}"


def query(base_url: str, workspace_id: str) -> str:
    return _workspace_base(base_url, workspace_id) + "/query"


def schema(base_url: str, workspace_id: str) -> str:
    return _workspace_base(base_url, workspace_id) + "/schema"


def auth(base_url: str, workspace_id: str, action: str) -> str:
    return _workspace_base(base_url, workspace_id) + "/auth/" + action


def endpoint(base_url: str, workspace_id: str, slug: str) -> str:
    # A slug comes from the caller and lands in a path segment, so it is escaped rather than
    # trusted. safe="" so a slash cannot walk out of the segment.
    return _workspace_base(base_url, workspace_id) + "/endpoint/" + quote(slug, safe="")


def files(base_url: str, workspace_id: str, suffix: str = "") -> str:
    url = _workspace_base(base_url, workspace_id) + "/files"
    return url if not suffix else f"{url}/{suffix}"
