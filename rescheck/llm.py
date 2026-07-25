"""Native Anthropic SDK access for ResCheck.

Two things live here:

1. ``AnthropicProvider`` — implements the ``chat()`` shape that hiring-agent's
   ``models.LLMProvider`` protocol expects, so the upstream PDF-extraction and
   evaluator code can run unmodified against the Claude API.
2. ``call_json`` / ``call_text`` — direct helpers used by ResCheck's own advisor
   and LaTeX-rewriting steps.

The upstream repo ships an OpenAI-compatible HTTP shim; we deliberately don't
use it for Anthropic. Structured output, thinking and effort all have first
class support in the official SDK.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Effort is the cost/latency dial. Thinking stays on (the Claude Opus 5 default)
# everywhere; extraction is mechanical so it runs cheap, the rewrite is the hard
# reasoning step so it runs deep.
DEFAULT_MODELS = {
    "extract": "claude-opus-5",
    "score": "claude-opus-5",
    "improve": "claude-opus-5",
}
DEFAULT_EFFORT = {"extract": "low", "score": "medium", "improve": "high"}

_client: anthropic.Anthropic | None = None
_client_lock = threading.Lock()

# Schemas the API rejects get remembered so we degrade to prompt-only JSON once
# instead of paying for a failed request on every single call.
_schema_blocklist: set[str] = set()

# hiring-agent catches every exception inside its extraction loop and returns
# None, which loses the reason. Stash the real API error so callers can report
# "out of credits" instead of guessing at "maybe your PDF is a scan".
LAST_ERROR: BaseException | None = None


def api_error_hint() -> str | None:
    """A human explanation of the most recent API failure, if there was one."""
    exc = LAST_ERROR
    if exc is None:
        return None
    text = str(exc)
    if "credit balance is too low" in text:
        return (
            "The Anthropic account is out of credits — add some at "
            "console.anthropic.com under Plans & Billing, then run again."
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return "The ANTHROPIC_API_KEY in .env was rejected. Check or rotate it."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API even after retries. Try again shortly."
    return f"The Anthropic API call failed: {text[:300]}"

# JSON Schema keywords Anthropic structured outputs does not accept. Numeric and
# string constraints in particular are validated client-side by Pydantic anyway.
_UNSUPPORTED_SCHEMA_KEYS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "default", "examples", "deprecated",
    "readOnly", "writeOnly", "contentEncoding", "contentMediaType",
}


def get_client() -> anthropic.Anthropic:
    global _client
    with _client_lock:
        if _client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Put it in ResCheck/.env"
                )
            _client = anthropic.Anthropic(max_retries=4, timeout=600.0)
        return _client


def sanitize_schema(schema: Any) -> Any:
    """Make a Pydantic-generated JSON Schema acceptable to structured outputs.

    Strips unsupported validation keywords and forces ``additionalProperties:
    false`` on every object, which the API requires.
    """
    if isinstance(schema, list):
        return [sanitize_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    out = {
        key: sanitize_schema(value)
        for key, value in schema.items()
        if key not in _UNSUPPORTED_SCHEMA_KEYS
    }
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    return out


def _split_system(messages: list[dict[str, str]]) -> tuple[str | None, list[dict]]:
    """Anthropic takes the system prompt as its own argument, not a message."""
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    turns = [m for m in messages if m.get("role") != "system"]
    if not turns:
        turns = [{"role": "user", "content": "Proceed."}]
    return ("\n\n".join(system_parts) or None), turns


def _text_of(message: anthropic.types.Message) -> str:
    return "".join(b.text for b in message.content if b.type == "text")


def _request(
    *,
    model: str,
    system: str | None,
    messages: list[dict],
    max_tokens: int,
    effort: str,
    schema: dict | None = None,
    schema_key: str | None = None,
) -> str:
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "output_config": {"effort": effort},
    }
    if system:
        kwargs["system"] = system

    use_schema = schema is not None and schema_key not in _schema_blocklist
    if use_schema:
        kwargs["output_config"]["format"] = {
            "type": "json_schema",
            "schema": sanitize_schema(schema),
        }

    global LAST_ERROR
    try:
        with client.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
    except anthropic.BadRequestError as exc:
        # A 400 is either a schema the decoder won't accept or a hard account
        # problem like an exhausted balance. Only the former is worth retrying.
        if not use_schema or "credit balance" in str(exc):
            LAST_ERROR = exc
            raise
        # Almost always a schema the constrained decoder won't accept. Fall back
        # to prompt-enforced JSON, which every prompt in this app already asks
        # for, and stop trying this schema again.
        logger.warning("Structured output rejected (%s); falling back to prompt-only JSON", exc)
        if schema_key:
            _schema_blocklist.add(schema_key)
        kwargs["output_config"].pop("format", None)
        try:
            with client.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except Exception as retry_exc:
            LAST_ERROR = retry_exc
            raise
    except Exception as exc:
        LAST_ERROR = exc
        raise

    if message.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined this request: {message.stop_details}")
    if message.stop_reason == "max_tokens":
        logger.warning("Hit max_tokens on %s — output may be truncated", model)
    return _text_of(message)


def extract_json(text: str) -> Any:
    """Parse JSON out of a model response, tolerating stray prose or fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            raise
        return json.loads(text[start : end + 1])


def call_json(
    prompt: str,
    *,
    schema: dict,
    system: str | None = None,
    model: str = DEFAULT_MODELS["improve"],
    effort: str = "high",
    max_tokens: int = 16000,
) -> Any:
    raw = _request(
        model=model,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        effort=effort,
        schema=schema,
        schema_key=json.dumps(schema, sort_keys=True)[:512],
    )
    return extract_json(raw)


def call_text(
    prompt: str,
    *,
    system: str | None = None,
    model: str = DEFAULT_MODELS["improve"],
    effort: str = "high",
    max_tokens: int = 32000,
) -> str:
    return _request(
        model=model,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        effort=effort,
    )


class AnthropicProvider:
    """Adapter satisfying hiring-agent's ``LLMProvider`` protocol."""

    def __init__(self, model: str, effort: str = "medium", max_tokens: int = 16000):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        system, turns = _split_system(messages)
        schema = kwargs.get("format")
        content = _request(
            model=self.model,
            system=system,
            messages=turns,
            max_tokens=self.max_tokens,
            effort=self.effort,
            schema=schema if isinstance(schema, dict) else None,
            schema_key=(
                json.dumps(schema, sort_keys=True)[:512]
                if isinstance(schema, dict)
                else None
            ),
        )
        return {"message": {"role": "assistant", "content": content}}
