"""대조군 — 접지 않은 팔.

LLM 없이 돈다. :data:`~tablefold.t2sql.provider.Completer` 가 ``Prompt -> str``
이라 고정 문자열을 돌려주는 함수를 넘기면 전 경로가 검사된다.
"""

from __future__ import annotations

import pytest

from tablefold.report.baseline import (
    build_prompt,
    generate_without_fold,
    schema_ddl,
)


def test_schema_ddl_carries_columns_keys_and_relations(tiny_schema):
    ddl = schema_ddl(tiny_schema)
    assert "CREATE TABLE" in ddl
    for table in tiny_schema.tables:
        assert table.name in ddl
    assert "PRIMARY KEY" in ddl
    if tiny_schema.foreign_keys:
        assert "FOREIGN KEY" in ddl


def test_schema_ddl_keeps_comments(tiny_schema):
    """주석을 빼면 대조군이 접힌 레이어보다 적은 정보를 받는다.

    그러면 접힌 쪽이 이겨도 원인이 '접기' 인지 '설명' 인지 갈리지 않는다.
    """
    commented = [c for t in tiny_schema.tables for c in t.columns if c.comment]
    if not commented:
        pytest.skip("픽스처에 컬럼 주석이 없다")
    ddl = schema_ddl(tiny_schema)
    assert commented[0].comment in ddl


def test_prompt_prefix_is_stable_across_questions(tiny_schema):
    """캐시 접두사가 질문마다 달라지면 두 팔의 비용 비교가 무의미해진다."""
    a = build_prompt(tiny_schema, "매출 알려줘", dialect="tsql")
    b = build_prompt(tiny_schema, "주문 수 알려줘", dialect="tsql")
    assert a.cached == b.cached
    assert a.fresh != b.fresh


def test_first_answer_that_runs_is_taken(tiny_schema):
    calls = []

    def completer(prompt):
        calls.append(prompt)
        return "SELECT id FROM orders"

    result = generate_without_fold(
        "주문 번호", tiny_schema, completer=completer, executor=lambda sql: None
    )
    assert result.ok
    assert result.turns == 1
    assert len(calls) == 1


def test_execution_error_is_fed_back_and_retried(tiny_schema):
    """접힌 쪽만 자기수정을 가지면 이긴 원인이 접기인지 재시도인지 갈리지 않는다."""
    answers = iter(["SELECT nope FROM orders", "SELECT id FROM orders"])
    seen_prompts = []

    def completer(prompt):
        seen_prompts.append(str(prompt))
        return next(answers)

    def executor(sql):
        if "nope" in sql:
            raise RuntimeError("Invalid column name 'nope'")

    result = generate_without_fold(
        "주문 번호",
        tiny_schema,
        completer=completer,
        executor=executor,
        max_attempts=3,
    )
    assert result.ok
    assert result.turns == 2
    assert result.errors_seen == ("Invalid column name 'nope'",)
    assert "Invalid column name 'nope'" in seen_prompts[1]


def test_gives_up_at_the_attempt_cap_and_says_why(tiny_schema):
    def completer(prompt):
        return "SELECT nope FROM orders"

    def executor(sql):
        raise RuntimeError("Invalid column name 'nope'")

    result = generate_without_fold(
        "주문 번호",
        tiny_schema,
        completer=completer,
        executor=executor,
        max_attempts=2,
    )
    assert not result.ok
    assert result.sql is None
    assert result.turns == 2
    assert "nope" in result.error
    assert len(result.errors_seen) == 2


def test_unparseable_answer_counts_as_a_failed_turn(tiny_schema):
    def completer(prompt):
        return "미안, 모르겠다"

    result = generate_without_fold(
        "주문 번호", tiny_schema, completer=completer, max_attempts=1
    )
    assert not result.ok
    assert result.errors_seen  # 조용히 통과하지 않는다


def test_without_an_executor_the_first_parseable_answer_stands(tiny_schema):
    """실행 검증이 없으면 예전 엔진과 같은 동작 — 확장/파싱 통과가 곧 답이다."""
    result = generate_without_fold(
        "주문 번호",
        tiny_schema,
        completer=lambda p: "SELECT id FROM orders",
    )
    assert result.ok
    assert result.turns == 1
