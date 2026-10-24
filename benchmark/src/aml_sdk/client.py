"""A validating HTTP client for either SDK or existing native endpoints."""
from urllib.parse import urlsplit
import math
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .contracts import AddRequest, AddResponse, SearchRequest, SearchResponse, auth_headers, wire


class ContractError(ValueError):
    pass


class MemoryHTTPError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Memory endpoint returned HTTP {status_code}")


class MemoryClient:
    def __init__(self, base_url, *, auth_scheme="bearer", token=None, timeout=1800,
                 add_path="/add", search_path="/search", health_path="/health", transport=None,
                 max_attempts=32, sleep=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Use a plain HTTP(S) base URL without credentials, query or fragment")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 32:
            raise ValueError("max_attempts must be an integer between 1 and 32")
        self.max_attempts = max_attempts
        self.sleep = sleep or time.sleep
        for path in (add_path, search_path, health_path):
            if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
                raise ValueError("Endpoint paths must be local absolute paths")
        self.base_url = base_url.rstrip("/")
        self.paths = {"add": add_path, "search": search_path, "health": health_path}
        self.headers = auth_headers(auth_scheme, token)
        self.http = httpx.Client(timeout=httpx.Timeout(timeout, connect=min(10, timeout)),
                                 follow_redirects=False, trust_env=False, transport=transport)

    def _post(self, name, request, response_type):
        # Serialize once: a retry is the SAME logical operation, including IDs.
        payload = json.dumps(wire(request), ensure_ascii=False, separators=(",", ":")).encode()
        headers = {**self.headers, "Content-Type": "application/json"}
        retryable = {408, 425, 429, 500, 502, 503, 504}
        if name == "add":
            retryable |= {409, 524}
        for attempt in range(self.max_attempts):
            try:
                response = self.http.post(self.base_url + self.paths[name], content=payload, headers=headers)
            except httpx.TransportError:
                if attempt + 1 == self.max_attempts:
                    raise
                self.sleep(min(2 ** attempt, 60))
                continue
            if response.status_code == 200:
                break
            status = response.status_code
            delay = retry_delay(response, attempt)
            response.close()
            if status not in retryable or attempt + 1 == self.max_attempts:
                raise MemoryHTTPError(status)
            self.sleep(delay)
        try:
            return response_type.model_validate(response.json())
        except ValueError:
            raise ContractError(f"Invalid {name} response") from None
        finally:
            response.close()

    def add(self, request):
        request = request if isinstance(request, AddRequest) else AddRequest.model_validate(request)
        result = self._post("add", request, AddResponse)
        if any(getattr(result, key) != getattr(request, key) for key in ("request_id", "user_id", "session_id")):
            raise ContractError("Add did not echo the original request identifiers")
        return result

    def search(self, request):
        request = request if isinstance(request, SearchRequest) else SearchRequest.model_validate(request)
        result = self._post("search", request, SearchResponse)
        try:
            return result.validate_for(request)
        except ValueError:
            raise ContractError("Search exceeds top_k or repeats memory IDs") from None

    def health(self):
        # Health requests intentionally carry no Memory System Key.
        return self.http.get(self.base_url + self.paths["health"]).is_success

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def retry_delay(response, attempt):
    """AML memory-endpoint policy; unrelated to upstream model API retries."""
    if response.status_code != 429:
        return min(2 ** attempt, 60)
    value = response.headers.get("Retry-After", "").strip()
    try:
        # RFC Retry-After is integer seconds or an HTTP date, never a float.
        if value.isascii() and value.isdigit():
            return min(int(value), 60)
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            raise ValueError("Retry-After date needs a timezone")
        return min(max(0, (date - datetime.now(timezone.utc)).total_seconds()), 60)
    except (ValueError, TypeError, OverflowError):
        return 60
