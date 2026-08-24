"""질문 하나를 받아 실행 가능한 물리 SQL을 낸다.

```
질문 ──▶ 라우팅 프롬프트(카탈로그) ──▶ LLM ──▶ 모델 이름 하나
                                                    │
        작성 프롬프트(그 모델 하나 + 계약 + 예시) ◀──┘
                    │
                    ▼
                   LLM ──▶ 논리 SQL
                                │
            거부 이유 ◀──── expand (검사기)
                │              │
           수리 프롬프트        ▼
                           물리 SQL
```

**두 단계로 나눈 이유는 크기다.** 레이어 전체를 매번 넣으면 실측(NL2SQL 9모델)
26,744자인데, 질의가 실제로 읽는 것은 모델 하나뿐이다. 카탈로그로 하나를 고른 뒤
그 모델만 담으면 같은 답을 훨씬 적은 토큰으로 낸다. 대가는 호출 1회가 2회가 되는
것이고, 라우팅이 틀리면 그 질문을 못 푸는 것이다.

**그래서 후퇴 경로가 있다.** 라우팅이 아는 이름을 못 내거나, 고른 모델로 수리까지
다 써도 확장이 안 되면, 레이어 전체를 담은 프롬프트로 한 번 더 시도한다. 라우팅이
틀렸을 때 조용히 못 답하고 끝나는 것보다 낫다. 후퇴했다는 사실은
:attr:`GenerationResult.fell_back` 에 남는다.

**수리 루프.** :mod:`tablefold.rewrite.expand` 는 번역기가 아니라 검사기이고,
거부할 때 무엇이 왜 틀렸는지를 쓸 수 있는 이름 목록과 함께 낸다. 그 메시지를
사람이 읽고 마는 것과 모델에게 돌려주는 것의 차이가 크다 — "모르는 필드
`SALES_AMT`; 쓸 수 있는 것: `f_sales_SALES_AMT_sum`, …" 는 다음 시도에서 그대로
고쳐진다.

**실패를 성공처럼 돌려주지 않는다.** 후퇴까지 다 쓰고도 확장되지 않으면
:class:`GenerationError` 를 올리고 그 안에 시도 전부를 담는다.

**실행 검증.** 확장이 통과해도 데이터베이스가 거부할 수 있다 — 0 나눔,
방언 차이, 검사기가 모르는 제약. ``executor`` 를 주면 물리 SQL 을 실제로
실행해 보고, 예외로 거부하면 그 메시지를 수리 프롬프트에 넣어 다시 쓰게
한다. 화면에서 실행 실패가 사용자 앞에서 터지던 것을 생성 단계에서 끝내는
것이다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import sqlglot
from sqlglot import exp

from tablefold.fold import FoldResult
from tablefold.ir import LogicalLayer, LogicalModel
from tablefold.relate.graph import SchemaGraph
from tablefold.rewrite.expand import ExpansionError, FilterOnlyMisuse, expand
from tablefold.t2sql.parse import SQLNotFound, extract_sql
from tablefold.t2sql.prompt import (
    Example,
    Prompt,
    build_prompt,
    build_repair_prompt,
    build_router_prompt,
    parse_model_name,
    valid_examples,
)
from tablefold.t2sql.provider import (
    Completer,
    ReportsUsage,
    Usage,
    default_completer,
)

LOGGER = logging.getLogger("tablefold.t2sql")
"""단계 로그. 라이브러리는 기본적으로 아무것도 출력하지 않는다 —
:func:`tablefold.t2sql.trace.enable_logging` 이 켠다."""

DEFAULT_MAX_ATTEMPTS = 3
"""최초 시도 1회 + 수리 2회.

3회를 넘기면 같은 오류를 반복하는 쪽이 대부분이다. 거부 이유가 레이어에 없는
것을 요구하는 종류라면(4-4장의 앵커 문제) 몇 번을 더 물어도 답이 없다.
"""


@dataclass(frozen=True)
class Attempt:
    """한 번의 시도. 실패한 것도 남는다 — 왜 실패했는지가 결과의 일부다."""

    stage: str
    """``route`` · ``write`` · ``write (full layer)`` · ``execute``."""

    prompt: Prompt
    raw_response: str
    logical_sql: str | None
    error: str | None
    usage: Usage = Usage()

    fatal: bool = False
    """모델을 바꿔도 안 되는 거부인가. 필터 전용 오용이 그렇다 — 어느 모델에서도
    앵커 한 행에 대응하는 값이 없다."""

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class GenerationResult:
    question: str
    logical_sql: str
    physical_sql: str
    models_used: tuple[str, ...]
    fields_used: tuple[str, ...]
    joins_emitted: int
    joins_available: int
    routed_to: str | None = None
    """라우팅이 고른 모델. ``None`` 이면 라우팅이 아무것도 못 골랐다."""

    fell_back: bool = False
    """레이어 전체 프롬프트로 물러섰는지. 참이면 라우팅이 도움이 안 됐다."""

    rerouted_from: str | None = None
    """재라우팅 전 원래 골랐던 모델. 참이면 라우팅이 틀렸다가 오류로 바로잡힌
    것이고, ``routed_to`` 는 실제 답을 낸 모델을 가리킨다. 평가 스크립트가 이
    구분 없이는 "라우팅 적중률"을 과대계한다."""

    attempts: tuple[Attempt, ...] = field(default_factory=tuple)

    @property
    def joins_pruned(self) -> int:
        return self.joins_available - self.joins_emitted

    @property
    def calls(self) -> int:
        """LLM 호출 횟수. 라우팅 1회 + 작성 1회가 정상이다.

        실행 검증에 걸린 시도도 한 번의 호출에서 나온 것이므로 여기에 센다 —
        시도 하나가 호출 하나와 대응한다.
        """
        return len(self.attempts)

    @property
    def repairs(self) -> int:
        """확장이 통과하기까지 고쳐 쓴 횟수."""
        return sum(1 for a in self.attempts if a.stage.startswith("write")) - 1

    @property
    def executions(self) -> int:
        """실행 검증에서 걸려 되돌아온 횟수.

        확장이 통과해도 데이터베이스가 거부할 수 있다. :attr:`repairs` 는 작성
        단계의 고친 횟수만 세므로, 실행 검증이 몇 바퀴 돌았는지는 여기서 본다.
        """
        return sum(1 for a in self.attempts if a.stage == "execute")

    @property
    def usage(self) -> Usage:
        total = Usage()
        for attempt in self.attempts:
            total = total + attempt.usage
        return total


class GenerationError(RuntimeError):
    """후퇴까지 다 쓰고도 확장되는 SQL을 얻지 못했다."""

    def __init__(self, question: str, attempts: tuple[Attempt, ...]) -> None:
        self.question = question
        self.attempts = attempts
        last = next((a.error for a in reversed(attempts) if a.error), "시도 없음")
        super().__init__(f"{len(attempts)}번 호출 후에도 확장되지 않았다: {last}")


class TextToSQLEngine:
    """레이어 하나에 붙어 질문을 물리 SQL로 옮긴다.

    모델별 프롬프트 접두사는 생성 시점에 **한 번** 만들어 둔다. 프롬프트 캐시는
    바이트 단위 접두사 일치이므로, 질문마다 다시 만들면 같은 문자열이 나오더라도
    만드는 비용을 매번 낸다. 예시 검증도 여기서 한 번만 한다.

    ``route`` 를 끄면 예전처럼 레이어 전체를 한 번에 담는다 — 모델이 두세 개뿐인
    작은 레이어에서는 라우팅 호출값이 아까울 수 있다.

    ``executor`` 를 주면 확장이 통과한 물리 SQL 을 **실제로 실행** 해 본다.
    검사기가 아는 것은 이름 목록뿐이고, 0 나눔이나 방언 차이는 실행해 봐야 안다.
    호출은 SQL 문자열을 받아 성공하면 ``None``, 실패하면 무슨 예외든 던진다.
    예외를 던지면 그 메시지를 수리 프롬프트에 넣어 확장 거부와 똑같이 고친다.
    """

    def __init__(
        self,
        fold_result: FoldResult,
        *,
        completer: Completer | None = None,
        examples: tuple[Example, ...] = (),
        dialect: str = "postgres",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        route: bool = True,
        executor: Callable[[str], None] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")

        self.layer: LogicalLayer = fold_result.layer
        self.graph: SchemaGraph = fold_result.graph
        self.dialect = dialect
        self.max_attempts = max_attempts
        self.route = route and len(self.layer.models) > 1
        self.executor = executor
        self._completer = completer or default_completer()
        self.examples = valid_examples(
            examples, self.layer, self.graph, dialect=dialect
        )

    # ── 공개 ─────────────────────────────────────────────────────────────────

    def generate(self, question: str) -> GenerationResult:
        """*question* 에 답하는 물리 SQL. 실패하면 :class:`GenerationError`."""
        attempts: list[Attempt] = []

        chosen: LogicalModel | None = None
        if self.route:
            chosen = self._route(question, attempts)

        if chosen is not None:
            # 첫 실패에서 "다른 모델이 이 필드들을 더 많이 갖고 있다"가 보이면
            # 수리를 더 태우지 않는다. 고칠 수 있는 종류가 아니기 때문이다.
            alternative: list[LogicalModel] = []

            def _reroute(logical: str | None, _: str | None) -> bool:
                better = self._better_model(logical, chosen)
                if better is None:
                    return False
                LOGGER.info("   ↪ %s 가 더 맞는다 — 재라우팅", better.name)
                alternative.append(better)
                return True

            result = self._write(question, attempts, model=chosen, abandon_if=_reroute)
            if result is not None:
                return result

            if alternative:
                result = self._write(question, attempts, model=alternative[0])
                if result is not None:
                    result = replace(result, rerouted_from=_name(chosen))
                    return result

            if self._layer_cannot_help(attempts):
                # 필터 전용 오용은 어느 모델에서도 성립하지 않는다. 레이어 전체를
                # 다시 보여 줘도 같은 이유로 거부되므로 호출을 태우지 않는다.
                LOGGER.info("   ✗ 레이어를 바꿔도 안 되는 거부 — 후퇴 생략")
                raise GenerationError(question, tuple(attempts))

        result = self._write(
            question,
            attempts,
            model=None,
            routed_to=_name(chosen),
            fell_back=chosen is not None,
        )
        if result is None:
            raise GenerationError(question, tuple(attempts))
        return result

    # ── 판단 ─────────────────────────────────────────────────────────────────

    def _better_model(
        self, logical_sql: str | None, current: LogicalModel
    ) -> LogicalModel | None:
        """질의가 쓰려 한 이름을 *현재 모델보다* 더 많이 가진 모델.

        라우팅이 틀렸는지를 오류 문자열이 아니라 **질의가 실제로 요구한 것**으로
        판단한다. LLM 이 지어낸 이름이면 어느 모델도 더 갖고 있지 않으므로
        ``None`` 이 나오고, 그때는 같은 모델에서 고치는 게 맞다.

        전체 레이어로 후퇴하는 것보다 이쪽이 싸고 정확하다 — 프롬프트가 여전히
        모델 하나짜리다.
        """
        if not logical_sql:
            return None
        try:
            statement = sqlglot.parse_one(logical_sql, read=self.dialect)
        except Exception:  # noqa: BLE001 — 못 읽으면 판단 근거가 없다
            return None
        wanted = {c.name.lower() for c in statement.find_all(exp.Column) if c.name}
        if not wanted:
            return None

        def covered(model: LogicalModel) -> int:
            return len(wanted & {n.lower() for n in model.field_names})

        here = covered(current)
        ranked = sorted(
            (m for m in self.layer.models if m.name != current.name),
            key=lambda m: (-covered(m), m.name),
        )
        best = ranked[0] if ranked else None
        return best if best is not None and covered(best) > here else None

    def _layer_cannot_help(self, attempts: list[Attempt]) -> bool:
        """마지막 거부가 모델을 바꿔도 안 되는 종류인가."""
        return bool(attempts) and attempts[-1].fatal

    # ── 단계 ─────────────────────────────────────────────────────────────────

    def _route(self, question: str, attempts: list[Attempt]) -> LogicalModel | None:
        """카탈로그를 보여 주고 모델 하나를 고르게 한다. 실패하면 ``None``."""
        prompt = build_router_prompt(question, self.layer)
        LOGGER.info(
            "① 라우팅  모델 %d개 중 하나 · 프롬프트 %s",
            len(self.layer.models),
            _size(prompt),
        )
        LOGGER.debug("라우팅 프롬프트\n%s", prompt)
        raw = self._call(prompt)
        name = parse_model_name(raw, self.layer)
        LOGGER.info(
            "   %s  %s%s",
            "→" if name else "✗",
            name or f"모델을 고르지 못했다: {raw.strip()[:60]!r}",
            _usage(self._usage()),
        )
        attempts.append(
            Attempt(
                stage="route",
                prompt=prompt,
                raw_response=raw,
                logical_sql=None,
                error=None if name else f"모델을 고르지 못했다: {raw.strip()[:80]}",
                usage=self._usage(),
            )
        )
        return self.layer.model(name) if name else None

    def _write(
        self,
        question: str,
        attempts: list[Attempt],
        *,
        model: LogicalModel | None,
        routed_to: str | None = None,
        fell_back: bool = False,
        abandon_if: Callable[[str | None, str | None], bool] | None = None,
    ) -> GenerationResult | None:
        """SQL을 쓰게 하고, 거부되면 그 이유를 돌려주며 고쳐 쓰게 한다."""
        stage = "write" if model is not None else "write (full layer)"
        LOGGER.info(
            "② 작성    %s",
            f"{model.name} 하나만" if model is not None else "전체 레이어로 후퇴",
        )
        base = build_prompt(
            question,
            self.layer,
            self.graph,
            dialect=self.dialect,
            examples=self.examples,
            model=model,
        )
        prompt = base

        for turn in range(1, self.max_attempts + 1):
            LOGGER.debug("작성 프롬프트 (%d회차)\n%s", turn, prompt)
            raw = self._call(prompt)
            logical, error = self._read(raw)
            usage = self._usage()
            fatal = False
            LOGGER.info("   %d회차 프롬프트 %s%s", turn, _size(prompt), _usage(usage))

            if logical is not None and error is None:
                try:
                    expansion = expand(
                        logical, self.layer, self.graph, dialect=self.dialect
                    )
                except ExpansionError as exc:
                    error = str(exc)
                    fatal = isinstance(exc, FilterOnlyMisuse)
                else:
                    exec_error = self._verify(expansion.sql)
                    if exec_error is None:
                        LOGGER.info(
                            "   ✓ 확장 성공  모델 %s · 조인 %d/%d",
                            ", ".join(expansion.models_used),
                            expansion.joins_emitted,
                            expansion.joins_available,
                        )
                        attempts.append(
                            Attempt(stage, prompt, raw, logical, None, usage)
                        )
                        return GenerationResult(
                            question=question,
                            logical_sql=logical,
                            physical_sql=expansion.sql,
                            models_used=expansion.models_used,
                            fields_used=expansion.fields_used,
                            joins_emitted=expansion.joins_emitted,
                            joins_available=expansion.joins_available,
                            routed_to=_name(model) or routed_to,
                            fell_back=fell_back,
                            attempts=tuple(attempts),
                        )
                    # 확장은 통과했지만 데이터베이스가 거부했다. 시도는 execute
                    # 단계로 남긴다 — :attr:`GenerationResult.repairs` 가 작성
                    # 단계의 수정만 세도록 하기 위해서다. 실행 검증이 몇 바퀴
                    # 돌았는지는 executions 가 센다.
                    LOGGER.info("   ✗ 실행 거부  %s", exec_error[:120])
                    attempts.append(
                        Attempt("execute", prompt, raw, logical, exec_error, usage)
                    )
                    if abandon_if is not None and abandon_if(logical, exec_error):
                        return None
                    # 고칠 대상은 검사기를 통과한 물리 SQL 이다. 실행이 거부한
                    # 그 문자열을 그대로 돌려준다.
                    prompt = build_repair_prompt(
                        base,
                        rejected_sql=expansion.sql,
                        error=exec_error,
                    )
                    continue

            LOGGER.info("   ✗ 거부  %s", (error or "")[:120])
            attempts.append(Attempt(stage, prompt, raw, logical, error, usage, fatal))
            if abandon_if is not None and abandon_if(logical, error):
                return None
            prompt = build_repair_prompt(
                base,
                # SQL 을 못 꺼냈으면 고칠 대상이 완성 텍스트 자체다.
                rejected_sql=logical if logical is not None else raw,
                error=error or "알 수 없는 오류",
            )

        return None

    # ── 잡일 ─────────────────────────────────────────────────────────────────

    def _verify(self, physical_sql: str) -> str | None:
        """실행 검증. 통과하면 ``None``, 거부하면 오류 메시지.

        검증자가 없으면 아무것도 묻지 않는다 — 확장 통과가 곧 최종 답인 예전
        동작이다. 예외 메시지가 빈 문자열이면 시도가 ``ok`` 로 둔갑하므로
        예외 이름으로 대신한다.
        """
        if self.executor is None:
            return None
        try:
            self.executor(physical_sql)
        except Exception as exc:  # noqa: BLE001 — 무슨 예외든 실행 실패다
            return str(exc) or type(exc).__name__
        return None

    def _call(self, prompt: Prompt) -> str:
        return self._completer(prompt)

    def _usage(self) -> Usage:
        if isinstance(self._completer, ReportsUsage):
            return self._completer.last_usage() or Usage()
        return Usage()

    def _read(self, raw: str) -> tuple[str | None, str | None]:
        try:
            return extract_sql(raw, dialect=self.dialect), None
        except SQLNotFound as exc:
            return None, str(exc)


def _name(model: LogicalModel | None) -> str | None:
    return model.name if model is not None else None


def _size(prompt: Prompt) -> str:
    return f"{len(prompt.cached):,}자(캐시) + {len(prompt.fresh):,}자"


def _usage(usage: Usage) -> str:
    if not (usage.input_tokens or usage.cached_tokens or usage.output_tokens):
        return ""
    return (
        f" · 토큰 {usage.input_tokens:,}+{usage.cached_tokens:,}캐시"
        f"/{usage.output_tokens:,}"
    )


def generate_sql(
    question: str,
    fold_result: FoldResult,
    *,
    completer: Completer | None = None,
    examples: tuple[Example, ...] = (),
    dialect: str = "postgres",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    route: bool = True,
) -> GenerationResult:
    """한 번 쓰고 버릴 때. 질문이 여럿이면 :class:`TextToSQLEngine` 을 재사용한다."""
    engine = TextToSQLEngine(
        fold_result,
        completer=completer,
        examples=examples,
        dialect=dialect,
        max_attempts=max_attempts,
        route=route,
    )
    return engine.generate(question)
