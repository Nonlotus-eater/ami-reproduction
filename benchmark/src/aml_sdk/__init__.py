"""Textual AML integration, without a shared storage or model implementation."""

from .backend import Backend, BackendError
from .contracts import AddRequest, AddResponse, MemoryItem, SearchRequest, SearchResponse

__all__ = ["Backend", "BackendError", "AddRequest", "AddResponse", "MemoryItem",
           "SearchRequest", "SearchResponse"]
