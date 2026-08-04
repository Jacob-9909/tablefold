"""A completion callable, for callers who do not already have one.

:class:`~tablefold.select.LLMSelector` takes any ``str -> str``, which is the
whole interface it needs and keeps the core free of a vendor SDK. This module is
the convenience path: one adapter, behind the ``llm`` extra, so ``--llm`` on the
command line works without the caller writing glue.

Anything else — a different provider, a cached client, a proxy, a recorded
fixture — is a plain function and needs nothing from here.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = "claude-sonnet-4-5"

# Enough for a few dozen anchor names and their model names. The reply is JSON
# and short; the prompt is what carries the size.
DEFAULT_MAX_TOKENS = 2048


class LLMUnavailable(RuntimeError):
    """The SDK or the credential needed for a completion is missing."""


def anthropic_completer(
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
):
    """Return a ``str -> str`` completion function backed by the Anthropic API.

    Raises :class:`LLMUnavailable` at construction rather than at call time, so
    a missing SDK or key fails before a fold has been half-run.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise LLMUnavailable(
            "the anthropic package is not installed; "
            "install the 'llm' extra (uv sync --extra llm)"
        ) from exc

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable("no API key; set ANTHROPIC_API_KEY or pass api_key=")

    client = anthropic.Anthropic(api_key=key)

    def complete(prompt: str) -> str:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")

    return complete
