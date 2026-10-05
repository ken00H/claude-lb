"""Anthropic Messages API wire models and error envelopes.

The downstream `/v1/messages` surface speaks Anthropic natively: requests are
validated against the Messages API shape and every refusal uses the Anthropic
error envelope (``{"type": "error", "error": {"type", "message"}}``), never the
OpenAI envelope the codex-era routers emit.
"""

from __future__ import annotations

from typing import Any, Final, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.types import JsonObject, JsonValue

ANTHROPIC_ERROR_ENVELOPE_TYPE: Final = "error"

AnthropicErrorType = Literal[
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "rate_limit_error",
    "api_error",
    "overloaded_error",
]


class MessageContentPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None
    source: dict[str, Any] | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["user", "assistant"]
    content: str | list[MessageContentPart]


class MessagesRequest(BaseModel):
    """The subset of the Anthropic Messages API that gates account selection.

    Extra fields pass through untouched (native passthrough shaping): the
    upstream owns the full schema, so this model only enforces what the proxy
    needs to route and refuses before any account is selected.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    max_tokens: int = Field(strict=True, ge=1)
    stream: bool | None = None
    system: str | list[MessageContentPart] | None = None
    metadata: dict[str, Any] | None = None

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)


def anthropic_error(
    error_type: AnthropicErrorType | str,
    message: str,
) -> dict[str, JsonValue]:
    """The Anthropic error envelope for downstream responses."""
    return {
        "type": ANTHROPIC_ERROR_ENVELOPE_TYPE,
        "error": {"type": error_type, "message": message},
    }


def anthropic_validation_error(exc: ValidationError) -> dict[str, JsonValue]:
    first = exc.errors()[0] if exc.errors() else {}
    loc = first.get("loc", [])
    param = ".".join(str(part) for part in loc if part != "body")
    message = "Invalid request payload"
    if isinstance(first.get("msg"), str) and first["msg"]:
        message = first["msg"]
    if param:
        message = f"{message} ({param})"
    return anthropic_error("invalid_request_error", message)


def parse_anthropic_error_payload(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Extract ``(error_type, message)`` from an Anthropic error envelope."""
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None, None
    error_type = error.get("type")
    message = error.get("message")
    return (
        error_type if isinstance(error_type, str) else None,
        message if isinstance(message, str) else None,
    )


def as_anthropic_error_envelope(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Coerce an internal error payload (OpenAI- or Anthropic-shaped) into the
    Anthropic envelope for the `/v1/messages` boundary."""
    if payload.get("type") == ANTHROPIC_ERROR_ENVELOPE_TYPE and isinstance(payload.get("error"), Mapping):
        return dict(payload)  # type: ignore[return-value]
    error_type, message = parse_anthropic_error_payload(payload)
    if error_type is None and isinstance(payload.get("error"), Mapping):
        # OpenAI-shaped internal payload: {"error": {"code"|"type", "message"}}.
        error = payload["error"]
        code = error.get("code")
        if isinstance(code, str) and code:
            error_type = code
        message = message or (error.get("message") if isinstance(error.get("message"), str) else None)
    return anthropic_error(error_type or "api_error", message or "Request failed")
