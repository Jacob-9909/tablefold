"""Anthropic LLM 완결 어댑터 모듈.

:class:`~tablefold.choose.select.LLMSelector`는 프롬프트와 텍스트 반환을 담당하는
단순한 ``str -> str`` 함수를 요구합니다. 이 모듈은 CLI의 ``--llm`` 플래그 사용 시
손쉽게 Anthropic API를 연동할 수 있는 편의 어댑터를 제공합니다.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = "claude-sonnet-4-5"

# Enough for a few dozen anchor names and their model names. The reply is JSON
# and short; the prompt is what carries the size.
DEFAULT_MAX_TOKENS = 2048


class LLMUnavailable(RuntimeError):
    """LLM 완료 호출에 필요한 SDK나 API 키 등의
    인증 정보가 누락된 경우 발생하는 예외."""


def anthropic_completer(
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
):
    """Anthropic API 기반의 ``str -> str`` 완결 함수를 반환합니다."""
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
