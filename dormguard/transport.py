"""Bounded JSON provider transport. Exceptions intentionally omit URLs/tokens/body."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RemoteError(Exception):
    def __init__(self, status: int = 503, retry_after: int | None = None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(
            "External provider unavailable" if status >= 500 else "External provider rejected request"
        )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_seconds(data, default=30):
    """Telegram's retry_after is a minimum waiting period, never a capped delay."""
    parameters = data.get("parameters") if isinstance(data, dict) else None
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    return value if type(value) is int and 1 <= value <= 2**31 - 1 else default


def request_json(method: str, url: str, headers=None, body=None, timeout=10):
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(
        url, data=payload, method=method, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RemoteError(502)
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise RemoteError(502)
            return result
    except HTTPError as exc:
        retry = None
        if exc.code == 429:
            try:
                data = json.loads(exc.read(65536))
                retry = retry_seconds(data)
            except (OSError, ValueError, TypeError, AttributeError):
                retry = 30
        raise RemoteError(exc.code, retry) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise RemoteError(503) from None
