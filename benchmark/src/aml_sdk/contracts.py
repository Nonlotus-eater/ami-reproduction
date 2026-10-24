"""Textual AML wire types; requests are strict, response extensions are ignored."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

Text = Annotated[str, Field(min_length=1)]


class WireModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("*", check_fields=False)
    @classmethod
    def reject_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Empty text is not supported")
        return value


class Message(WireModel):
    # system is a local-benchmark extension; official user/assistant inputs are
    # passed through identically. No role conversion is performed.
    role: Literal["user", "assistant", "system"]
    content: Text
    timestamp: int | None = None


class AddRequest(WireModel):
    request_id: Text
    user_id: Text
    session_id: Text
    messages: Annotated[list[Message], Field(min_length=1)]


class ResponseModel(WireModel):
    # AML consumes only the declared response fields. Existing native servers
    # may include metadata; accepting it does not alter their memory evidence.
    model_config = ConfigDict(extra="ignore")


class AddResponse(ResponseModel):
    success: StrictBool
    request_id: Text
    user_id: Text
    session_id: Text

    @field_validator("success")
    @classmethod
    def must_succeed(cls, value):
        if value is not True:
            raise ValueError("An Add success response must contain true")
        return value


class SearchRequest(WireModel):
    query: Text
    user_id: Text
    top_k: Annotated[int, Field(gt=0)]
    options: list[Text] | None = None

    @field_validator("options")
    @classmethod
    def nonblank_options(cls, value):
        if value is not None and any(not option.strip() for option in value):
            raise ValueError("Options must contain text")
        return value


class MemoryItem(ResponseModel):
    id: Text
    content: Text
    score: float | None = None
    created_at: Text | None = None


class SearchResponse(ResponseModel):
    data: list[MemoryItem]

    def validate_for(self, request: SearchRequest):
        ids = [item.id for item in self.data]
        if len(ids) > request.top_k or len(ids) != len(set(ids)):
            raise ValueError("Search exceeds top_k or repeats memory IDs")
        return self


def wire(value: WireModel):
    return value.model_dump(mode="json", exclude_none=True)


def auth_headers(scheme, token):
    if scheme == "none":
        return {}
    if scheme not in {"bearer", "token", "x-api-key"}:
        raise ValueError("Unknown authentication scheme")
    if not token or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Set a nonempty ASCII API key through the token environment variable")
    return ({"X-Api-Key": token} if scheme == "x-api-key" else
            {"Authorization": ("Bearer" if scheme == "bearer" else "Token") + " " + token})
