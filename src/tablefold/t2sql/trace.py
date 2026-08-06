"""한 질문이 안에서 무슨 일을 겪었는지 보여 준다.

두 가지가 필요하다. 돌아가는 **동안** 보이는 것과, 끝난 **뒤에** 되짚는 것이다.

* **로깅** — :mod:`tablefold.t2sql.engine` 이 단계마다 ``logging`` 으로 남긴다.
  긴 실행에서 지금 어디쯤인지 알려면 스트리밍이어야 한다. :func:`enable_logging`
  이 그걸 켠다.
* **추적** — :class:`~tablefold.t2sql.engine.GenerationResult` 의 ``attempts`` 에
  이미 프롬프트·응답·오류·토큰이 다 들어 있다. :func:`render_trace` 는 그것을
  사람이 읽는 표로 편다. 실패한 실행도 :class:`GenerationError` 안에 같은 것이
  들어 있으므로 똑같이 읽을 수 있다 — **실패했을 때가 로그가 제일 필요한 때다.**

프롬프트는 기본적으로 크기만 보여 준다. 전문은 ``full=True``. 26,744자짜리를
터미널에 쏟으면 정작 보려던 흐름이 안 보인다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from tablefold.t2sql.engine import Attempt, GenerationError, GenerationResult

LOGGER = logging.getLogger("tablefold.t2sql")

_STAGE_MARK = {
    "route": "①",
    "write": "②",
    "write (full layer)": "②↩",
}


def enable_logging(level: int = logging.INFO, *, stream=None) -> None:
    """단계 로그를 켠다. 라이브러리는 기본적으로 아무것도 출력하지 않는다.

    ``DEBUG`` 로 두면 프롬프트 전문과 완성 텍스트 원본까지 나온다.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def render_trace(
    outcome: GenerationResult | GenerationError, *, full: bool = False
) -> str:
    """한 질문의 전 과정. 성공이든 실패든 같은 모양으로 읽는다."""
    if isinstance(outcome, GenerationError):
        return "\n".join(
            [
                f"질문: {outcome.question}",
                f"결과: 실패 — {outcome}",
                "",
                *_attempt_lines(outcome.attempts, full=full),
            ]
        )

    head = [
        f"질문: {outcome.question}",
        f"결과: {outcome.models_used[0] if outcome.models_used else '?'} "
        f"· 조인 {outcome.joins_emitted}/{outcome.joins_available}"
        f" · 호출 {outcome.calls}회 · 수리 {outcome.repairs}회",
    ]
    if outcome.routed_to:
        routed = f"라우팅: {outcome.routed_to}"
        if outcome.fell_back:
            routed += " → 실패, 전체 레이어로 후퇴"
        head.append(routed)
    elif outcome.calls > 1:
        head.append("라우팅: 모델을 고르지 못함 → 전체 레이어")

    usage = outcome.usage
    if usage.input_tokens or usage.cached_tokens:
        head.append(
            f"토큰: 입력 {usage.input_tokens:,} + 캐시 {usage.cached_tokens:,}"
            f" / 출력 {usage.output_tokens:,}"
            f" (캐시 적중 {usage.cache_hit_rate * 100:.0f}%)"
        )

    return "\n".join([*head, "", *_attempt_lines(outcome.attempts, full=full)])


def _attempt_lines(attempts: Iterable[Attempt], *, full: bool) -> list[str]:
    lines: list[str] = []
    for index, attempt in enumerate(attempts, 1):
        mark = _STAGE_MARK.get(attempt.stage, "·")
        status = "✓" if attempt.ok else "✗"
        lines.append(
            f"[{index}] {mark} {attempt.stage:<18} {status}  "
            f"프롬프트 {len(attempt.prompt.cached):,}자(캐시 경계)"
            f" + {len(attempt.prompt.fresh):,}자"
            f"{_usage_note(attempt)}"
        )
        if full:
            lines.append(_block("프롬프트(캐시 접두사)", attempt.prompt.cached))
            lines.append(_block("프롬프트(질문 꼬리)", attempt.prompt.fresh))
        lines.append(_block("응답", attempt.raw_response, cap=None if full else 400))
        if attempt.logical_sql and attempt.stage != "route":
            lines.append(_block("논리 SQL", attempt.logical_sql))
        if attempt.error:
            lines.append(_block("거부", attempt.error))
        lines.append("")
    return lines


def _usage_note(attempt: Attempt) -> str:
    usage = attempt.usage
    if not (usage.input_tokens or usage.cached_tokens or usage.output_tokens):
        return ""
    return (
        f"  · 토큰 {usage.input_tokens:,}+{usage.cached_tokens:,}캐시"
        f"/{usage.output_tokens:,}"
    )


def _block(label: str, text: str, *, cap: int | None = None) -> str:
    body = text.strip()
    if cap is not None and len(body) > cap:
        body = f"{body[:cap]}\n    … {len(text) - cap:,}자 생략 (--trace-full)"
    indented = "\n".join(f"    {line}" for line in body.splitlines())
    return f"    ── {label} ──\n{indented}"
