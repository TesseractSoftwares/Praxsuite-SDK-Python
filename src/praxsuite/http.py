"""The one place this SDK talks to the network.

Built on ``urllib.request`` from the standard library, so ``pip install praxsuite`` pulls in
nothing. That is a deliberate trade: requests/httpx would be more pleasant to write against, but
a backend SDK that drags a transitive dependency tree into every project causes version conflicts
in exactly the environments Python people care about - a locked service image, a Lambda bundle, a
notebook someone else has to reproduce.

What that costs us is connection pooling. ``urllib`` opens a connection per request, so a hot
loop is slower than it would be with a pooled client. If that matters for your workload, batch
with ``in_()`` filters and larger page sizes rather than more requests.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from .errors import PraxError, PraxNetworkError, PraxTimeoutError
from .log import logger
from .result import parse_error, parse_json_or_none

__all__ = ["HttpTransport"]

#: Retries are only ever attempted for transient failures, and only for idempotent requests.
#: Retrying a failed insert is how you get two rows.
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.4


class HttpTransport:
    """Sends JSON requests and returns parsed JSON, or raises a PraxError."""

    def __init__(self, timeout: float = 20.0, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        # No global opener: installing one would change urllib's behaviour for the whole process,
        # which is not a library's decision to make.
        self._opener = urllib.request.build_opener()

    def request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Any = None,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        """Sends one request, retrying transient failures when ``retry_safe``.

        Raises:
            PraxError: on any failure, already classified.
        """
        attempts = self.max_attempts if retry_safe else 1
        last: PraxError | None = None

        for attempt in range(attempts):
            if attempt:
                delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.debug("Retrying in %.1fs (attempt %d of %d)", delay, attempt + 1, attempts)
                time.sleep(delay)
            try:
                return self._send_once(method, url, headers, body)
            except PraxError as exc:
                last = exc
                if not exc.is_transient:
                    raise

        assert last is not None
        raise last

    def _send_once(
        self, method: str, url: str, headers: Mapping[str, str], body: Any
    ) -> dict[str, Any]:
        payload = None
        request_headers = dict(headers)
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.setdefault("Accept", "application/json")
        request_headers.setdefault("Accept-Encoding", "gzip")

        request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
        logger.debug("-> %s %s", method, url)

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                raw = self._read(response)
        except urllib.error.HTTPError as exc:
            # An HTTPError IS the response, so the gateway's error body is readable from it.
            raw = self._read(exc)
            raise parse_error(exc.code, raw) from None
        except urllib.error.URLError as exc:
            raise self._transport_error(exc) from None
        except TimeoutError:
            raise PraxTimeoutError(
                "TIMEOUT", "The request timed out before the gateway answered."
            ) from None

        logger.debug("<- %d %s (%d bytes)", status, url, len(raw))

        # 204 and an empty 200 are both legitimate: a logout returns no body.
        if not raw.strip():
            return {}

        parsed = parse_json_or_none(raw)
        if not isinstance(parsed, dict):
            raise PraxError(
                "MALFORMED_RESPONSE",
                f"The gateway returned HTTP {status} with a body that is not a JSON object.",
                status,
                (),
                raw,
            )
        return parsed

    @staticmethod
    def _read(response: Any) -> str:
        data = response.read()
        # urllib does not decompress for us, and we asked for gzip.
        if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        return str(data.decode("utf-8", errors="replace"))

    @staticmethod
    def _transport_error(exc: urllib.error.URLError) -> PraxError:
        reason = exc.reason
        text = str(reason)
        if isinstance(reason, TimeoutError) or "timed out" in text.lower():
            return PraxTimeoutError("TIMEOUT", "The request timed out before the gateway answered.")
        if "certificate" in text.lower() or "ssl" in text.lower():
            return PraxNetworkError(
                "NETWORK_ERROR",
                f"TLS failed talking to the gateway: {text}. On a stripped container image this "
                f"is usually missing CA certificates.",
            )
        return PraxNetworkError("NETWORK_ERROR", f"Could not reach the gateway: {text}")
