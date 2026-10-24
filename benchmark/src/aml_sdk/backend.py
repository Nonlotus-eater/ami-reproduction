"""Structural backend protocol; persistence and idempotency belong to methods."""
from importlib import import_module, util
import hashlib
from pathlib import Path
import re
from typing import Protocol

from .contracts import AddRequest, MemoryItem, SearchRequest


class Backend(Protocol):
    def add(self, request: AddRequest) -> None: ...
    def search(self, request: SearchRequest) -> list[MemoryItem]: ...
    def close(self) -> None: ...


class BackendError(Exception):
    """An intentionally public error code, never a provider exception body."""
    def __init__(self, status_code: int, reason: str, *, retry_after: int | None = None):
        if type(status_code) is not int or not 400 <= status_code <= 599 or not re.fullmatch(r"[a-z_]{1,80}", reason):
            raise ValueError("Use an HTTP error status and a safe snake_case reason")
        super().__init__(reason)
        self.status_code, self.reason = status_code, reason
        self.retry_after = retry_after


def load_factory(entry: str):
    """Load module:Factory or /path/to/backend.py:Factory without a registry."""
    module, separator, name = entry.rpartition(":")
    if not separator or not module or not name.isidentifier():
        raise ValueError("Backend entry must be module:Factory or file.py:Factory")
    if module.endswith(".py"):
        path = Path(module).resolve(strict=True)
        spec = util.spec_from_file_location("aml_backend_" + hashlib.sha256(str(path).encode()).hexdigest(), path)
        loaded = util.module_from_spec(spec)
        # Register before execution so dataclasses and forward annotations work.
        import sys
        sys.modules[spec.name] = loaded
        spec.loader.exec_module(loaded)
    else:
        loaded = import_module(module)
    factory = getattr(loaded, name)
    if not callable(factory):
        raise TypeError("Backend factory must be callable")
    return factory


class HTTPBackend:
    """Optional bridge to an existing service; its database remains untouched."""
    def __init__(self, config):
        import os
        from .client import MemoryClient
        self.client = MemoryClient(config["base_url"],
            auth_scheme=config.get("auth_scheme", "bearer"),
            token=os.environ.get(config.get("token_env", "AML_UPSTREAM_API_KEY")),
            timeout=config.get("timeout", 1800),
            # A proxy preserves failures for the evaluating client to retry.
            # Avoid nesting 32 upstream attempts inside 32 caller attempts.
            max_attempts=config.get("max_attempts", 1),
            add_path=config.get("add_path", "/add"), search_path=config.get("search_path", "/search"),
            health_path=config.get("health_path", "/health"))

    def add(self, request):
        self._call(self.client.add, request)

    def search(self, request):
        return self._call(self.client.search, request).data

    @staticmethod
    def _call(function, request):
        from .client import ContractError, MemoryHTTPError
        try:
            return function(request)
        except MemoryHTTPError as error:
            raise BackendError(error.status_code, "upstream_failed") from None
        except ContractError:
            raise BackendError(502, "invalid_upstream_response") from None

    def health(self):
        return self.client.health()

    def close(self):
        self.client.close()
