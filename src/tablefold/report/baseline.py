"""대조군 — tablefold 없이 같은 질문에 답하게 한다.

:mod:`tablefold.report` 의 나머지는 전부 **접기 자체**를 잰다. 얼마나 줄었는지
(:mod:`~tablefold.report.compression`), 무엇이 남았는지
(:mod:`~tablefold.report.fidelity`), 무엇을 답할 수 있는지
(:mod:`~tablefold.report.answerable`). 전부 접은 결과의 성질이다.

그런데 이 프로젝트의 주장은 그게 아니다. 주장은 **"Text-to-SQL 이 실패하는 원인은
모델이 아니라 스키마 컨텍스트"** 이고, 그 주장은 접지 않았을 때와 견주어야만
증명된다. 압축률은 접기가 무엇을 했는지 말하지, 그게 도움이 됐는지는 말하지 않는다.

이 모듈이 그 반대편이다. 같은 LLM, 같은 질문, 같은 실행 검증에 **원본 DDL** 을
주고 물리 SQL 을 직접 쓰게 한다.

**왜 '항등 폴드'가 아닌가.** 표 하나를 모델 하나로 만든 레이어를 태우면 코드가
훨씬 적어진다. 그런데 :func:`~tablefold.rewrite.expand._reject_multiple_models`
가 한 질의의 다중 모델 참조를 거부한다 — 와이드 모델을 지키려고 있는 가드다.
항등 레이어에서는 모든 조인 질문이 그 가드에 걸려 죽고, 골드셋의 대부분이
조인을 필요로 하므로 대조군이 0점을 받는다. 접기가 이긴 게 아니라 대조군의
손발을 묶은 것이다. 그래서 확장 단계를 통째로 지나친다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tablefold.ir import PhysicalSchema
from tablefold.t2sql.prompt import Prompt
from tablefold.t2sql.provider import Completer

DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class BaselineResult:
    question: str
    sql: str | None
    turns: int
    error: str | None = None
    prompt_chars: int = 0
    errors_seen: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.sql is not None and self.error is None


def schema_ddl(schema: PhysicalSchema) -> str:
    """물리 스키마를 ``CREATE TABLE`` 문으로 되돌린다.

    대조군이 보는 화면이다. 접힌 레이어 대신 이걸 프롬프트에 넣는다.

    주석을 함께 싣는다 — 빼면 대조군에게서 접힌 레이어는 가지고 있는 정보를
    빼앗는 셈이라, 이긴 이유가 '접기' 인지 '설명을 줬는지' 인지 갈리지 않는다.
    """
    blocks: list[str] = []
    for table in schema.tables:
        lines = [f"CREATE TABLE {table.qualified_name} ("]
        parts = []
        for column in table.columns:
            piece = f"    {column.name} {column.type}"
            if not column.nullable:
                piece += " NOT NULL"
            if column.comment:
                piece += f"  -- {column.comment}"
            parts.append(piece)
        if table.primary_key:
            parts.append(f"    PRIMARY KEY ({', '.join(table.primary_key)})")
        lines.append(",\n".join(parts))
        lines.append(");")
        if table.comment:
            lines.insert(0, f"-- {table.name}: {table.comment}")
        blocks.append("\n".join(lines))

    for fk in schema.foreign_keys:
        blocks.append(
            f"ALTER TABLE {fk.from_table} ADD FOREIGN KEY "
            f"({', '.join(fk.from_columns)}) REFERENCES {fk.to_table} "
            f"({', '.join(fk.to_columns)});"
        )
    return "\n\n".join(blocks)


_CONTRACT = """\
너는 {dialect} SQL 을 쓴다. 위 스키마의 표와 컬럼만 쓴다.

1. 답은 실행 가능한 SELECT 문 하나다. 설명·주석·마크다운 없이 SQL 만 낸다.
2. 없는 표나 컬럼을 지어내지 않는다.
3. 1:N 관계를 조인한 뒤 부모 쪽 값을 SUM 하면 자식 행 수만큼 부풀어 오른다.
   그럴 때는 자식을 먼저 집계한 뒤 조인한다.
"""


def build_prompt(schema: PhysicalSchema, question: str, *, dialect: str) -> Prompt:
    """접힌 레이어 대신 원본 DDL 을 넣은 프롬프트.

    :class:`~tablefold.t2sql.prompt.Prompt` 를 그대로 쓴다 — 캐시 경계가 같아야
    두 팔의 토큰 비용을 나란히 놓고 볼 수 있고, 같은
    :data:`~tablefold.t2sql.provider.Completer` 를 태울 수 있다.
    """
    cached = f"{schema_ddl(schema)}\n\n{_CONTRACT.format(dialect=dialect)}"
    return Prompt(cached=cached, fresh=f"질문: {question}\nSQL:")


def generate_without_fold(
    question: str,
    schema: PhysicalSchema,
    *,
    completer: Completer,
    dialect: str = "postgres",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    executor: Callable[[str], None] | None = None,
    extract: Callable[[str, str], str] | None = None,
) -> BaselineResult:
    """원본 DDL 만 주고 물리 SQL 을 쓰게 한다. 실패하면 오류를 물려 다시 묻는다.

    재시도 상한을 엔진과 같은 값으로 두는 이유는, 접힌 쪽만 자기수정을 갖고
    있으면 이긴 원인이 접기인지 재시도인지 갈리지 않기 때문이다.
    """
    from tablefold.t2sql.parse import extract_sql

    take = extract or (lambda raw, d: extract_sql(raw, dialect=d))
    prompt = build_prompt(schema, question, dialect=dialect)
    seen: list[str] = []

    for turn in range(1, max_attempts + 1):
        raw = completer(prompt)
        try:
            sql = take(raw, dialect)
        except Exception as exc:  # noqa: BLE001 — 파싱 실패도 한 번의 실패다
            error = str(exc) or type(exc).__name__
        else:
            error = _run(executor, sql)
            if error is None:
                return BaselineResult(
                    question=question,
                    sql=sql,
                    turns=turn,
                    prompt_chars=len(prompt),
                    errors_seen=tuple(seen),
                )
        seen.append(error)
        prompt = Prompt(
            cached=prompt.cached,
            fresh=(
                f"질문: {question}\n"
                f"직전 답이 실패했다: {error}\n"
                "원인을 고쳐 SQL 만 다시 낸다.\nSQL:"
            ),
        )

    return BaselineResult(
        question=question,
        sql=None,
        turns=max_attempts,
        error=seen[-1] if seen else "no attempt produced SQL",
        prompt_chars=len(prompt),
        errors_seen=tuple(seen),
    )


def _run(executor: Callable[[str], None] | None, sql: str) -> str | None:
    if executor is None:
        return None
    try:
        executor(sql)
    except Exception as exc:  # noqa: BLE001 — 무슨 예외든 실행 실패다
        return str(exc) or type(exc).__name__
    return None
