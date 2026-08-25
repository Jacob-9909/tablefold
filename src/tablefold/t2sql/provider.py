"""프롬프트를 받아 완성 텍스트를 돌려주는 호출자.

``Completer`` 는 ``Prompt -> str`` 이다. 벤더 SDK 를 엔진 안으로 끌고 들어오지
않기 위해서다 — 테스트는 고정 문자열을 돌려주는 함수를 넘기면 되고, 이 모듈은
:mod:`tablefold.t2sql.engine` 이 없어도 혼자 이해된다.

**프롬프트 캐시.** :class:`~tablefold.t2sql.prompt.Prompt` 가 이미 캐시 접두사와
휘발 꼬리를 갈라 놓았으므로, 여기서는 그 경계를 공급자의 방식으로 표현하기만
하면 된다.

* **Anthropic** — 명시적이다. ``cache_control: {"type": "ephemeral"}`` 을 마지막
  system 블록에 붙인다. 렌더 순서가 tools → system → messages 라서, system 에
  붙인 breakpoint 가 그 앞 전부를 함께 캐시한다. 질문은 ``messages`` 로 가므로
  접두사를 건드리지 않는다. ``usage.cache_read_input_tokens`` 로 확인한다.
* **OpenAI** — 자동이다. 마커가 없고, 접두사가 같으면 알아서 붙는다. 그래서
  이쪽에서 할 일은 **순서를 지키는 것**뿐 — 안정적인 것이 앞, 질문이 뒤.
  ``usage.prompt_tokens_details.cached_tokens`` 로 확인한다.

두 공급자 모두 **최소 길이**가 있다. 접두사가 그보다 짧으면 캐시는 에러 없이 그냥
안 붙는다. 모델 하나만 담는 프롬프트는 작아서 이 문턱 아래로 내려갈 수 있다 —
:class:`CacheReport` 가 실제로 붙었는지 재는 자리인 이유가 그것이다.

**키가 없으면 가짜로 대신하지 않는다.** 한때 키워드로 SQL 을 지어내는 대체
구현이 있었고, 그러면 벤치마크가 "성공"을 출력하면서 실제로는 LLM 을 한 번도
부르지 않은 숫자를 낸다. 읽는 사람은 엔진 성능을 보고 있다고 믿는데 사실은
if 문의 성능이다. ``demo/live.py`` 가 접속 실패에 가짜 스키마를 안 쓰는 것과
같은 이유다 — 없으면 없다고 말하는 편이 낫다.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from dotenv import load_dotenv

from tablefold.t2sql.prompt import Prompt

# Load .env file automatically
load_dotenv()

Completer = Callable[[Prompt], str]

# 이 이름은 계정·시점에 따라 실존하지 않을 수 있다. 호출이 404 로 돌아오면
# :func:`_model_error` 가 TABLEFOLD_LLM_MODEL 로 바꾸라는 안내를 붙여 준다.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"

# 논리 SQL 은 길지 않다. 넉넉하되 무한하지 않게.
DEFAULT_MAX_TOKENS = 2048

# 라우팅 답은 모델 이름 하나다.
ROUTER_MAX_TOKENS = 32

# 벤치마크 재현성. 온도를 생략하면 공급자 기본값(대개 1)이 적용되어 같은
# 프롬프트가 실행마다 다른 SQL 을 내고, 흔들리는 숫자는 측정이 아니라 추론이
# 된다. 결정적 동작을 비결정적 동작보다 선호하는 것이 이 저장소의 원칙이다.
DETERMINISTIC_TEMPERATURE = 0


def anthropic_kwargs(prompt: Prompt, *, model: str, max_tokens: int) -> dict:
    """Anthropic Messages API 요청 본문. 순수 함수다.

    SDK 호출 안에 이 값을 인라인으로 박아 두면 테스트가 온도와 캐시 설정을
    볼 수 없었다. 여기서 만들고 completer 는 전달만 한다.
    """
    return {
        "model": model,
        "max_tokens": max_tokens,
        # 같은 프롬프트는 같은 답을 내야 한다 — 생략하면 공급자 기본값이
        # 적용되어 라우팅·작성이 실행마다 흔들린다.
        "temperature": DETERMINISTIC_TEMPERATURE,
        # 캐시 breakpoint. system 은 messages 보다 먼저 렌더되므로 여기 붙이면
        # 접두사 전체가 캐시되고, 질문은 뒤에 있어 접두사를 바꾸지 않는다.
        "system": [
            {
                "type": "text",
                "text": prompt.cached,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": prompt.fresh}],
    }


def openai_kwargs(prompt: Prompt, *, model: str, max_tokens: int) -> dict:
    """OpenAI 챗 완성 요청 본문. 순수 함수다.

    신형 추론 모델(o 계열, gpt-5 계열)은 ``max_tokens`` 를 아예 거부하고
    ``max_completion_tokens`` 만 받는다. 보내고 실패한 뒤 재시도하는 대신
    이름만 보고 미리 고른다. 벤더 접두사(``openrouter/openai/gpt-4o`` 같은)
    뒷부분을 기준으로 판단한다 — 접두사째로 보면 ``o`` 로 시작해 버린다.
    """
    bare = model.rsplit("/", 1)[-1]
    token_kwarg = (
        "max_completion_tokens" if bare.startswith(("o", "gpt-5")) else "max_tokens"
    )
    return {
        "model": model,
        token_kwarg: max_tokens,
        # 같은 프롬프트는 같은 답을 내야 한다 — :func:`anthropic_kwargs` 참고.
        "temperature": DETERMINISTIC_TEMPERATURE,
        "messages": [
            {"role": "system", "content": prompt.cached},
            {"role": "user", "content": prompt.fresh},
        ],
    }


@dataclass(frozen=True)
class Usage:
    """호출 한 번의 토큰 사용량.

    ``cached`` 가 계속 0 이면 캐시가 안 붙고 있다는 뜻이다. 원인은 대개 접두사
    안에 매번 달라지는 것이 섞였거나, 접두사가 최소 길이보다 짧은 것이다.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )

    @property
    def cache_hit_rate(self) -> float:
        """입력 토큰 중 캐시에서 온 비율."""
        total = self.input_tokens + self.cached_tokens
        return self.cached_tokens / total if total else 0.0


@runtime_checkable
class ReportsUsage(Protocol):
    """마지막 호출의 사용량을 돌려줄 수 있는 completer.

    선택 사항이다. 평범한 함수나 람다는 이걸 만족하지 않고, 엔진은 그런 경우
    사용량을 비워 둔다 — 테스트가 completer 를 한 줄로 쓰는 것을 막지 않으려고
    필수로 두지 않았다.
    """

    def last_usage(self) -> Usage | None: ...


class ProviderUnavailable(RuntimeError):
    """호출 가능한 공급자가 없다. 키가 없거나 SDK 가 설치되지 않았다."""


# 공급자 API 가 "그런 모델 없음"으로 답하는 패턴. 404 상태 코드와 메시지
# 문구를 함께 본다 — SDK 마다 예외 모양이 달라서 문자열이 가장 이식성이 좋다.
_MODEL_NOT_FOUND_MARKERS = ("404", "not_found", "does not exist", "no such model")


def _model_error(exc: Exception, model: str) -> ProviderUnavailable | None:
    """모델 이름 문제면 행동 지침을 담은 예외로 바꾼다. 아니면 ``None``.

    ``claude-sonnet-5`` 같은 기본값은 계정·시점에 따라 실존하지 않을 수 있다.
    원문 오류만 올리면 "SDK 가 고장 났나?" 의심하게 되지만, 실제로는 이름
    하나 바꾸면 끝나는 일이다 — 어디를 바꿔야 하는지까지 같이 말한다.
    """
    text = str(exc).lower()
    if not any(marker in text for marker in _MODEL_NOT_FOUND_MARKERS):
        return None
    return ProviderUnavailable(
        f"모델 '{model}' 을(를) 찾을 수 없습니다. "
        "TABLEFOLD_LLM_MODEL 환경 변수로 사용 가능한 모델을 지정하세요."
    )


class _Recording:
    """마지막 호출의 사용량을 들고 있는 completer 의 공통 부분."""

    def __init__(self) -> None:
        self._last: Usage | None = None

    def last_usage(self) -> Usage | None:
        return self._last


class AnthropicCompleter(_Recording):
    """캐시 breakpoint 를 명시적으로 놓는다."""

    def __init__(self, client, model: str, max_tokens: int) -> None:
        super().__init__()
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def __call__(self, prompt: Prompt) -> str:
        try:
            message = self._client.messages.create(
                **anthropic_kwargs(
                    prompt, model=self._model, max_tokens=self._max_tokens
                )
            )
        except Exception as exc:
            handled = _model_error(exc, self._model)
            if handled is not None:
                raise handled from exc
            raise
        usage = message.usage
        self._last = Usage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
        return "".join(b.text for b in message.content if b.type == "text")


class OpenAICompleter(_Recording):
    """캐시는 자동이다. 접두사 순서를 지키는 것이 전부다."""

    def __init__(self, client, model: str, max_tokens: int) -> None:
        super().__init__()
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def __call__(self, prompt: Prompt) -> str:
        try:
            response = self._client.chat.completions.create(
                **openai_kwargs(prompt, model=self._model, max_tokens=self._max_tokens)
            )
        except Exception as exc:
            handled = _model_error(exc, self._model)
            if handled is not None:
                raise handled from exc
            raise
        usage = response.usage
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        total_input = getattr(usage, "prompt_tokens", 0) or 0
        self._last = Usage(
            # 캐시된 몫을 빼서 "새로 낸 입력"만 남긴다. 두 공급자의 숫자가
            # 같은 뜻을 갖게 하려는 것이다 — Anthropic 의 input_tokens 는
            # 이미 캐시 읽기를 뺀 값이다.
            input_tokens=max(total_input - cached, 0),
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cached_tokens=cached,
        )
        return response.choices[0].message.content or ""


def anthropic_completer(
    *, model: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS
) -> Completer:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ProviderUnavailable("ANTHROPIC_API_KEY 가 설정되어 있지 않다")
    try:
        import anthropic
    except ImportError as exc:
        raise ProviderUnavailable("anthropic 패키지가 없다: uv add anthropic") from exc

    return AnthropicCompleter(
        anthropic.Anthropic(api_key=key),
        model or _configured_model() or DEFAULT_ANTHROPIC_MODEL,
        max_tokens,
    )


def openai_completer(
    *, model: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS
) -> Completer:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderUnavailable("OPENAI_API_KEY 가 설정되어 있지 않다")
    try:
        import openai
    except ImportError as exc:
        raise ProviderUnavailable("openai 패키지가 없다: uv add openai") from exc

    base_url = os.environ.get("OPENAI_API_BASE") or os.environ.get("LLM_BASE_URL")
    kwargs: dict[str, str] = {"api_key": key}
    if base_url:
        kwargs["base_url"] = base_url

    return OpenAICompleter(
        openai.OpenAI(**kwargs),
        model or _configured_model() or DEFAULT_OPENAI_MODEL,
        max_tokens,
    )


def _configured_model() -> str | None:
    return os.environ.get("LLM_MODEL") or os.environ.get("TABLEFOLD_LLM_MODEL")


def default_completer(
    *, model: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS
) -> Completer:
    """쓸 수 있는 공급자를 찾는다. 없으면 :class:`ProviderUnavailable`.

    키가 있는 쪽을 먼저 본다. 둘 다 없으면 어느 쪽이 왜 안 됐는지 함께 말한다 —
    "공급자 없음"만 나오면 키를 안 넣은 것인지 SDK 를 안 깐 것인지 모른다.
    """
    builders = (
        (openai_completer, anthropic_completer)
        if os.environ.get("OPENAI_API_KEY")
        else (anthropic_completer, openai_completer)
    )
    reasons: list[str] = []
    for build in builders:
        try:
            return build(model=model, max_tokens=max_tokens)
        except ProviderUnavailable as exc:
            reasons.append(str(exc))
    raise ProviderUnavailable("; ".join(reasons))
