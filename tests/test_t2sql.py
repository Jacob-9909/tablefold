"""t2sql 엔진 — 파싱, 프롬프트, 수리 루프, 프리셋."""

from __future__ import annotations

from pathlib import Path

import pytest

from tablefold.ir import (
    ForeignKey,
    LogicalLayer,
    PhysicalColumn,
    PhysicalSchema,
    PhysicalTable,
)
from tablefold.read.ddl import DDLIntrospector
from tablefold.relate.graph import SchemaGraph
from tablefold.t2sql import (
    Example,
    GenerationError,
    SQLNotFound,
    TextToSQLEngine,
    build_prompt,
    build_router_prompt,
    extract_sql,
    fold_star_schema,
    generate_sql,
    parse_model_name,
    recover_relationships,
    render_catalog,
    split_anchors,
    valid_examples,
)
from tablefold.t2sql.goldset import GoldCase, schema_gap

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "enterprise_bi.sql"

STAR_DDL = """
CREATE TABLE D_ORG (
    ORG_CD VARCHAR(100) PRIMARY KEY,
    HEAD_NM VARCHAR(100)
);
CREATE TABLE D_ITEM (
    ITEM_CD VARCHAR(100) PRIMARY KEY,
    ITEM_NM VARCHAR(100)
);
CREATE TABLE F_SALES (
    YYYYMMDD VARCHAR(8),
    ORG_CD VARCHAR(100),
    ITEM_CD VARCHAR(100),
    SALES_AMT NUMERIC(18,2),
    PRIMARY KEY (YYYYMMDD, ORG_CD, ITEM_CD),
    FOREIGN KEY (ORG_CD) REFERENCES D_ORG(ORG_CD),
    FOREIGN KEY (ITEM_CD) REFERENCES D_ITEM(ITEM_CD)
);
CREATE TABLE F_PLAN (
    YYYYMMDD VARCHAR(8),
    ORG_CD VARCHAR(100),
    PLAN_AMT NUMERIC(18,2),
    PRIMARY KEY (YYYYMMDD, ORG_CD),
    FOREIGN KEY (ORG_CD) REFERENCES D_ORG(ORG_CD)
);
"""


@pytest.fixture
def star():
    return DDLIntrospector(STAR_DDL).introspect()


@pytest.fixture
def star_fold(star):
    return fold_star_schema(star)


def replying(*responses: str):
    """고정 답을 순서대로 돌려주는 completer. 다 쓰면 마지막 것을 반복한다."""
    queue = list(responses)

    def complete(_: str) -> str:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return complete


# ── 파싱 ──────────────────────────────────────────────────────────────────────


def test_fenced_sql_is_preferred_over_surrounding_prose():
    reply = "물론이죠!\n```sql\nSELECT a FROM orders\n```\n도움이 되었길 바랍니다."

    assert extract_sql(reply) == "SELECT a FROM orders"


def test_bare_sql_without_a_fence_is_found():
    assert extract_sql("SELECT a, b FROM orders WHERE a = 1") == (
        "SELECT a, b FROM orders WHERE a = 1"
    )


def test_trailing_prose_is_trimmed_until_the_sql_parses():
    """모델은 펜스를 빼먹고 뒤에 설명을 붙인다. 꼬리를 줄여 파싱되는 데까지 간다."""
    reply = "SELECT a FROM orders\n이 쿼리는 주문을 모두 가져옵니다."

    assert extract_sql(reply) == "SELECT a FROM orders"


def test_a_reply_with_no_sql_is_an_error():
    with pytest.raises(SQLNotFound):
        extract_sql("죄송하지만 그 질문에는 답할 수 없습니다.")


def test_a_bare_word_is_not_mistaken_for_sql():
    """``sqlglot`` 은 낱말 하나도 식으로 파싱한다. 질의만 받아야 한다."""
    with pytest.raises(SQLNotFound):
        extract_sql("```sql\nunknown\n```")


# ── 프리셋 ────────────────────────────────────────────────────────────────────


def test_anchors_split_on_structure_not_on_name(star):
    """참조당하면 차원, 참조만 하면 팩트. 이름 접두사를 보지 않는다."""
    from tablefold.relate.graph import SchemaGraph

    dimensions, facts = split_anchors(SchemaGraph.build(star))

    assert set(dimensions) == {"D_ORG", "D_ITEM"}
    assert set(facts) == {"F_SALES", "F_PLAN"}


def test_the_star_preset_answers_across_two_facts(star_fold):
    """차원 앵커가 있어야 "매출 대 계획"이 조인 없이 풀린다."""
    org = star_fold.layer.model("D_ORG")

    assert org is not None
    names = {f.name for f in org.fields}
    assert "f_sales_SALES_AMT_sum" in names
    assert "f_plans_PLAN_AMT_sum" in names


def test_the_star_preset_also_keeps_a_fact_grain(star_fold):
    """전표 한 줄 단위의 상세는 팩트 앵커라야 답한다."""
    sales = star_fold.layer.model("F_SALES")

    assert sales is not None
    assert "SALES_AMT" in {f.name for f in sales.fields}


def test_recovery_adds_no_edge_when_keys_are_already_declared(star):
    """선언된 키가 이미 있으면 복구가 같은 엣지를 또 만들지 않는다."""
    assert recover_relationships(star).foreign_keys == star.foreign_keys


def test_recovery_finds_an_undeclared_reference():
    ddl = """
    CREATE TABLE D_ORG (ORG_CD VARCHAR(100) PRIMARY KEY, HEAD_NM VARCHAR(100));
    CREATE TABLE D_ITEM (ITEM_CD VARCHAR(100) PRIMARY KEY, ITEM_NM VARCHAR(100));
    CREATE TABLE F_STOCK (
        YYYYMM VARCHAR(6),
        ORG_CD VARCHAR(100),
        ITEM_CD VARCHAR(100),
        STOCK_AMT NUMERIC(18,2),
        PRIMARY KEY (YYYYMM, ORG_CD, ITEM_CD),
        FOREIGN KEY (ITEM_CD) REFERENCES D_ITEM(ITEM_CD)
    );
    """
    schema = DDLIntrospector(ddl).introspect()

    recovered = recover_relationships(schema)

    added = set(recovered.foreign_keys) - set(schema.foreign_keys)
    assert {(fk.from_table, fk.to_table) for fk in added} == {("F_STOCK", "D_ORG")}


# ── 프롬프트 ──────────────────────────────────────────────────────────────────


def test_an_example_that_does_not_expand_is_dropped(star_fold):
    """검사가 아니라 실행으로 거른다. 거부당할 SQL을 모범으로 보여 주면 안 된다."""
    good = Example("매출", "SELECT SALES_AMT FROM F_SALES")
    bad = Example("없는 필드", "SELECT NO_SUCH_FIELD FROM F_SALES")

    kept = valid_examples((good, bad), star_fold.layer, star_fold.graph)

    assert kept == (good,)


def test_a_filter_only_example_is_dropped(star_fold):
    """필드 이름만 확인하면 통과하지만 확장은 거부하는 예시."""
    org = star_fold.layer.model("D_ORG")
    filter_only = next(f.name for f in org.fields if f.filter_only)

    kept = valid_examples(
        (Example("q", f"SELECT {filter_only} FROM D_ORG"),),
        star_fold.layer,
        star_fold.graph,
    )

    assert kept == ()


def test_the_prompt_carries_the_layer_and_the_question(star_fold):
    prompt = build_prompt("매출 알려줘", star_fold.layer, star_fold.graph)

    assert "매출 알려줘" in str(prompt)
    assert "f_sales_SALES_AMT_sum" in str(prompt)
    assert "JOIN 하지 않는다" in str(prompt)


def test_the_question_stays_out_of_the_cached_prefix(star_fold):
    """캐시는 접두사 일치다. 질문이 접두사에 섞이면 매 질문이 캐시 미스가 된다."""
    first = build_prompt("매출 알려줘", star_fold.layer, star_fold.graph)
    second = build_prompt("계획 알려줘", star_fold.layer, star_fold.graph)

    assert first.cached == second.cached
    assert "매출 알려줘" not in first.cached
    assert "매출 알려줘" in first.fresh


def test_a_repair_reuses_the_same_cached_prefix(star_fold):
    """수리 시도가 접두사를 새로 만들면 캐시를 못 쓴다."""
    from tablefold.t2sql.prompt import build_repair_prompt

    base = build_prompt("매출", star_fold.layer, star_fold.graph)
    repair = build_repair_prompt(base, rejected_sql="SELECT 1", error="nope")

    assert repair.cached == base.cached
    assert "nope" in repair.fresh


def test_a_single_model_prompt_is_smaller_than_the_whole_layer(star_fold):
    """단계를 나눈 이유가 이것이다 — 질의는 모델 하나만 읽는다."""
    whole = build_prompt("매출", star_fold.layer, star_fold.graph)
    one = build_prompt(
        "매출",
        star_fold.layer,
        star_fold.graph,
        model=star_fold.layer.model("F_SALES"),
    )

    assert len(one) < len(whole)


def test_a_single_model_prompt_only_names_that_model(star_fold):
    one = build_prompt(
        "매출",
        star_fold.layer,
        star_fold.graph,
        model=star_fold.layer.model("F_SALES"),
        examples=(
            Example("팩트", "SELECT SALES_AMT FROM F_SALES"),
            Example("차원", "SELECT HEAD_NM FROM D_ORG"),
        ),
    )

    assert "### F_SALES" in one.cached
    assert "### D_ORG" not in one.cached
    # 다른 모델을 읽는 예시는 이 프롬프트에서 모범이 될 수 없다.
    assert "FROM D_ORG" not in one.cached


# ── 라우팅 ────────────────────────────────────────────────────────────────────


def test_the_catalog_names_every_model_without_listing_every_field(star_fold):
    catalog = render_catalog(star_fold.layer)

    for model in star_fold.layer.models:
        assert model.name in catalog
    # 카탈로그가 레이어 전체만큼 커지면 나눈 뜻이 없다.
    assert len(catalog) < len(build_prompt("q", star_fold.layer, star_fold.graph))


def test_a_model_name_is_read_out_of_a_chatty_reply(star_fold):
    assert parse_model_name("D_ORG", star_fold.layer) == "D_ORG"
    assert parse_model_name("  `D_ORG`  ", star_fold.layer) == "D_ORG"
    assert parse_model_name("이 질문은 D_ORG 로 답합니다.", star_fold.layer) == "D_ORG"


def test_an_invented_model_name_is_refused(star_fold):
    """지어낸 이름이 다음 단계로 흘러가면 거기서 터진다. 여기서 막는다."""
    assert parse_model_name("SALES_SUMMARY", star_fold.layer) is None


def test_the_router_prompt_keeps_the_question_out_of_the_prefix(star_fold):
    prompt = build_router_prompt("매출 알려줘", star_fold.layer)

    assert "매출 알려줘" not in prompt.cached
    assert "매출 알려줘" in prompt.fresh


# ── 엔진 ──────────────────────────────────────────────────────────────────────


def test_a_valid_answer_expands_on_the_first_attempt(star_fold):
    engine = TextToSQLEngine(
        star_fold,
        completer=replying("```sql\nSELECT HEAD_NM FROM D_ORG\n```"),
        route=False,
    )

    result = engine.generate("조직 이름")

    assert result.models_used == ("D_ORG",)
    assert result.repairs == 0
    assert "WITH tf__D_ORG" in result.physical_sql


def test_a_rejected_answer_is_repaired_with_the_expansion_error(star_fold):
    """확장의 거부 메시지는 사람이 읽으라고 쓴 것이 아니라 고치라고 쓴 것이다."""
    prompts: list[str] = []

    def completer(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "```sql\nSELECT NOPE FROM D_ORG\n```"
        return "```sql\nSELECT HEAD_NM FROM D_ORG\n```"

    result = TextToSQLEngine(star_fold, completer=completer, route=False).generate(
        "조직"
    )

    assert result.repairs == 1
    assert len(result.attempts) == 2
    assert result.attempts[0].error is not None
    # 두 번째 프롬프트가 첫 시도의 SQL과 그 거부 이유를 들고 있어야 한다.
    assert "SELECT NOPE FROM D_ORG" in str(prompts[1])
    assert "unknown fields" in str(prompts[1])


def test_exhausted_attempts_raise_rather_than_return_unusable_sql(star_fold):
    """실패를 성공처럼 돌려주면 호출자는 실행 가능한 SQL을 받았다고 믿는다."""
    engine = TextToSQLEngine(
        star_fold,
        completer=replying("```sql\nSELECT NOPE FROM D_ORG\n```"),
        max_attempts=2,
        route=False,
    )

    with pytest.raises(GenerationError) as raised:
        engine.generate("조직")

    assert len(raised.value.attempts) == 2
    assert all(not a.ok for a in raised.value.attempts)


def test_a_reply_with_no_sql_is_repaired_too(star_fold):
    """SQL을 못 꺼낸 것도 고칠 수 있는 실패다. 완성 텍스트를 그대로 돌려준다."""
    replies = iter(["답할 수 없습니다.", "```sql\nSELECT HEAD_NM FROM D_ORG\n```"])

    result = TextToSQLEngine(
        star_fold, completer=lambda _: next(replies), route=False
    ).generate("조직")

    assert result.repairs == 1


def test_joins_are_pruned_to_what_the_query_asked_for(star_fold):
    result = generate_sql(
        "품목별 매출",
        star_fold,
        route=False,
        completer=replying(
            "```sql\nSELECT ITEM_NM, SUM(SALES_AMT) AS AMT "
            "FROM F_SALES GROUP BY ITEM_NM\n```"
        ),
    )

    # 모델은 D_ORG 와 D_ITEM 둘 다 안지만 질의는 품목만 썼다.
    assert result.joins_emitted < result.joins_available
    assert "D_ORG" not in result.physical_sql


def test_routing_picks_a_model_then_writes_against_only_that_model(star_fold):
    """정상 경로: 호출 2회. 두 번째 프롬프트에는 고른 모델만 들어 있다."""
    seen: list = []

    def completer(prompt):
        seen.append(prompt)
        if len(seen) == 1:
            return "D_ORG"
        return "```sql\nSELECT HEAD_NM FROM D_ORG\n```"

    result = TextToSQLEngine(star_fold, completer=completer).generate("조직별 매출")

    assert result.calls == 2
    assert result.routed_to == "D_ORG"
    assert result.fell_back is False
    assert result.repairs == 0
    # 작성 프롬프트는 고른 모델 하나만 담는다.
    assert "### D_ORG" in seen[1].cached
    assert "### F_SALES" not in seen[1].cached


def test_a_router_that_names_nothing_falls_back_to_the_whole_layer(star_fold):
    """라우팅이 실패했다고 질문까지 실패시키지 않는다."""
    replies = iter(
        ["글쎄요, 잘 모르겠습니다.", "```sql\nSELECT HEAD_NM FROM D_ORG\n```"]
    )
    prompts: list = []

    def completer(prompt):
        prompts.append(prompt)
        return next(replies)

    result = TextToSQLEngine(star_fold, completer=completer).generate("조직")

    assert result.routed_to is None
    assert result.attempts[0].error is not None
    # 후퇴한 프롬프트는 레이어 전체를 담는다.
    assert "### F_SALES" in prompts[1].cached


def test_routing_is_skipped_when_the_layer_has_one_model(star_fold):
    """모델이 하나뿐이면 고를 것이 없다 — 라우팅 호출값이 낭비다."""
    from dataclasses import replace

    only_sales = replace(
        star_fold,
        layer=replace(star_fold.layer, models=(star_fold.layer.model("F_SALES"),)),
    )
    calls: list = []

    def completer(prompt):
        calls.append(prompt)
        return "```sql\nSELECT SALES_AMT FROM F_SALES\n```"

    result = TextToSQLEngine(only_sales, completer=completer).generate("매출")

    assert result.calls == 1
    # 라우팅을 안 했으니 고른 모델도 없다 — 후퇴한 것과는 다르다.
    assert result.routed_to is None
    assert result.fell_back is False


def test_max_attempts_must_be_positive(star_fold):
    with pytest.raises(ValueError):
        TextToSQLEngine(star_fold, completer=replying("x"), max_attempts=0)


# ── 골드셋 ────────────────────────────────────────────────────────────────────


def test_gold_references_resolve_table_aliases():
    case = GoldCase(
        case_id="SA_0002",
        question="q",
        concrete_question=None,
        gold_sql=(
            "SELECT B.HEAD_NM, SUM(A.SALES_AMT) FROM F_SALES A "
            "LEFT OUTER JOIN D_SA_ORG B ON A.ORG_CD = B.ORG_CD "
            "GROUP BY B.HEAD_NM",
        ),
        notes=(),
    )

    assert case.gold_tables == {"f_sales", "d_sa_org"}
    assert case.gold_references["f_sales"] == {"sales_amt", "org_cd"}
    assert case.gold_references["d_sa_org"] == {"head_nm", "org_cd"}
    assert case.subject == "SA"


def test_a_subquery_alias_is_not_read_as_a_physical_table():
    """정답은 서브쿼리에 ``X`` 를 붙이고 ``X.SALE_ACT`` 로 읽는다.

    물리 컬럼이 아니다.
    """
    case = GoldCase(
        case_id="SA_0002",
        question="q",
        concrete_question=None,
        gold_sql=(
            "SELECT X.HEAD_NM, SUM(X.SALE_ACT) FROM ("
            "SELECT B.HEAD_NM, SUM(A.SALES_AMT) AS SALE_ACT FROM F_SALES A "
            "JOIN D_SA_ORG B ON A.ORG_CD = B.ORG_CD GROUP BY B.HEAD_NM"
            ") X GROUP BY X.HEAD_NM",
        ),
        notes=(),
    )

    assert "x" not in case.gold_references
    assert "sale_act" not in case.gold_references.get("f_sales", set())


def test_the_schema_gap_names_columns_the_gold_sql_needs(star):
    case = GoldCase(
        case_id="PR_0001",
        question="q",
        concrete_question=None,
        gold_sql=("SELECT A.ORG_CD, A.MISSING_AMT FROM F_SALES A",),
        notes=(),
    )

    gap = schema_gap((case,), star)

    assert gap == {"f_sales": frozenset({"missing_amt"})}


# ── 픽스처 전체 ───────────────────────────────────────────────────────────────


@pytest.mark.skipif(not FIXTURE.exists(), reason="픽스처 없음")
def test_the_enterprise_fixture_folds_into_an_answerable_layer():
    """골드셋 스키마 19표가 팩트/차원 양쪽 앵커를 모두 갖춰야 한다."""
    schema = recover_relationships(DDLIntrospector.from_path(FIXTURE).introspect())
    result = fold_star_schema(schema)
    names = {m.name for m in result.layer.models}

    # 차원 앵커 — 팩트 둘을 나란히 놓는다.
    assert {"D_SA_ORG", "D_FI_ORG"} <= names
    # 팩트 앵커 — 전표 단위 상세.
    assert "F_SALES" in names
    assert result.layer.coverage == 1.0


@pytest.mark.skipif(not FIXTURE.exists(), reason="픽스처 없음")
def test_the_shipped_examples_expand_against_the_fixture():
    """예시가 깨지면 프롬프트에서 조용히 사라진다. 여기서 시끄럽게 깨져야 한다."""
    from tablefold.t2sql import NL2SQL_EXAMPLES

    schema = recover_relationships(DDLIntrospector.from_path(FIXTURE).introspect())
    result = fold_star_schema(schema)

    kept = valid_examples(NL2SQL_EXAMPLES, result.layer, result.graph)

    assert kept == NL2SQL_EXAMPLES


# ── 방언 ──────────────────────────────────────────────────────────────────────


def test_the_live_source_speaks_tsql():
    """라이브 소스는 MSSQL 이다. 기본값을 그대로 두면 실행이 깨진다.

    화면은 ``postgres`` 로 만들어 MSSQL 에 던졌고, "상위 10개" 질문마다
    ``Incorrect syntax near 'LIMIT'`` 로 죽었다. CLI 는 ``--mssql`` 에서 방언을
    바꿔서 ``OFFSET … FETCH NEXT`` 를 냈다 — 같은 질문, 다른 결과.
    """
    from tablefold.t2sql.prepare import dialect_for_live

    assert dialect_for_live("postgres") == "tsql"
    assert dialect_for_live("") == "tsql"
    assert dialect_for_live() == "tsql"


def test_an_explicitly_chosen_dialect_survives():
    """부르는 쪽이 일부러 고른 방언을 덮어쓰지 않는다."""
    from tablefold.t2sql.prepare import dialect_for_live

    assert dialect_for_live("duckdb") == "duckdb"
    assert dialect_for_live("tsql") == "tsql"


# ── 실행 검증 ────────────────────────────────────────────────────────────────


def test_an_execution_failure_is_repaired_with_the_runtime_error(star_fold):
    """확장 통과 ≠ 실행 가능. 데이터베이스가 거부하면 그 오류를 들고 다시 쓴다."""
    prompts: list[str] = []
    executed: list[str] = []

    def completer(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "```sql\nSELECT SALES_AMT / 0 FROM F_SALES\n```"
        return "```sql\nSELECT SALES_AMT FROM F_SALES\n```"

    def executor(sql: str) -> None:
        executed.append(sql)
        if "/ 0" in sql:
            raise ValueError("division by zero")

    result = TextToSQLEngine(
        star_fold,
        completer=completer,
        route=False,
        executor=executor,
    ).generate("매출")

    assert result.repairs == 0
    assert result.executions == 1
    assert result.calls == 2
    assert [a.stage for a in result.attempts] == ["execute", "write (full layer)"]
    assert not result.attempts[0].ok
    assert result.attempts[0].error == "division by zero"
    assert result.attempts[1].ok
    # 검증자는 물리 SQL 을 받았다.
    assert executed[0].startswith("WITH tf__F_SALES")
    # 수리 프롬프트는 거부한 물리 SQL 과 실행 오류를 함께 들고 간다.
    assert "division by zero" in str(prompts[1])
    assert "SALES_AMT / 0" in str(prompts[1])


def test_execution_failures_respect_max_attempts_and_raise(star_fold):
    """실행 검증도 수리 루프 안이다 — 한도를 넘으면 조용히 넘기지 않고 올린다."""
    calls: list[str] = []

    def executor(sql: str) -> None:
        calls.append(sql)
        raise ValueError("division by zero")

    engine = TextToSQLEngine(
        star_fold,
        completer=replying("```sql\nSELECT SALES_AMT / 0 FROM F_SALES\n```"),
        max_attempts=2,
        route=False,
        executor=executor,
    )

    with pytest.raises(GenerationError) as raised:
        engine.generate("매출")

    assert all(a.stage == "execute" for a in raised.value.attempts)
    assert len(calls) == 2
    assert all(not a.ok for a in raised.value.attempts)


def test_without_an_executor_an_expanded_answer_is_returned_untouched(star_fold):
    """검증자가 없으면 예전과 같다 — 확장 통과가 곧 최종 답이다.

    실행하면 반드시 깨질 SQL 도 그대로 나간다. 기본 동작이 조용히 바뀌면
    기존 호출자의 호출 횟수·비용이 몰래 늘어난다.
    """
    engine = TextToSQLEngine(
        star_fold,
        completer=replying("```sql\nSELECT SALES_AMT / 0 FROM F_SALES\n```"),
        route=False,
    )

    result = engine.generate("매출")

    assert result.executions == 0
    assert len(result.attempts) == 1
    assert result.calls == 1
    assert "/ 0" in result.physical_sql


# ── 공급자 요청 본문 ─────────────────────────────────────────────────────────


def _prompt():
    from tablefold.t2sql.prompt import Prompt

    return Prompt(cached="접두사", fresh="질문")


def test_both_providers_pin_temperature_to_zero():
    """온도를 생략하면 공급자 기본값이 적용되어 벤치마크가 흔들린다."""
    from tablefold.t2sql.provider import anthropic_kwargs, openai_kwargs

    anthropic = anthropic_kwargs(_prompt(), model="claude-sonnet-5", max_tokens=2048)
    openai = openai_kwargs(_prompt(), model="gpt-4o", max_tokens=2048)

    assert anthropic["temperature"] == 0
    assert openai["temperature"] == 0
    # 질문은 접두사 뒤에 있어야 캐시가 산다 — 헬퍼가 순서를 어기면 안 된다.
    assert anthropic["messages"] == [{"role": "user", "content": "질문"}]
    assert anthropic["system"][0]["text"] == "접두사"


def test_newer_openai_models_ask_for_max_completion_tokens():
    """신형 모델(o 계열 · gpt-5 계열)은 ``max_tokens`` 를 아예 거부한다."""
    from tablefold.t2sql.provider import openai_kwargs

    reasoning = openai_kwargs(_prompt(), model="o4-mini", max_tokens=2048)
    gpt5 = openai_kwargs(_prompt(), model="gpt-5", max_tokens=2048)
    legacy = openai_kwargs(_prompt(), model="gpt-4o", max_tokens=2048)
    prefixed = openai_kwargs(
        _prompt(), model="openrouter/openai/gpt-4o", max_tokens=2048
    )

    assert reasoning["max_completion_tokens"] == 2048
    assert "max_tokens" not in reasoning
    assert gpt5["max_completion_tokens"] == 2048
    # 옛 모델은 그대로다. 벤더 접두사가 ``o`` 로 시작한다고 신형 취급하면 안 된다.
    assert legacy["max_tokens"] == 2048
    assert "max_completion_tokens" not in legacy
    assert prefixed["max_tokens"] == 2048


def test_an_unknown_model_error_carries_the_override_hint():
    """404 가 원문으로 올라가면 SDK 고장처럼 읽힌다. 행동 지침을 붙인다."""

    from tablefold.t2sql.provider import ProviderUnavailable, _model_error

    original = Exception("Error code: 404 - model claude-sonnet-5 does not exist")
    handled = _model_error(original, "claude-sonnet-5")

    assert isinstance(handled, ProviderUnavailable)
    assert "TABLEFOLD_LLM_MODEL" in str(handled)


def test_other_errors_pass_through_untouched():
    """키 만료·네트워크 오류까지 모델 문제로 뭉개면 디버깅이 끝난다."""
    from tablefold.t2sql.provider import _model_error

    assert _model_error(Exception("invalid api key"), "gpt-4o") is None


def test_a_graph_turns_join_tables_into_compact_catalog_lines():
    """부모 둘을 참조하는 조합 표가 카탈로그를 수천 자 불리는 일이 없다.

    Mastodon 116표 실측: 카탈로그의 32% 가 조합 표 상세였다. 내용을 지우면
    관계 질문을 못 고르므로, 지우는 대신 관계 한 줄로 압축한다.
    """

    orders = PhysicalTable(
        name="orders",
        columns=(PhysicalColumn("order_id", "bigint", nullable=False),),
        primary_key=("order_id",),
    )
    customers = PhysicalTable(
        name="customers",
        columns=(PhysicalColumn("customer_id", "bigint", nullable=False),),
        primary_key=("customer_id",),
    )
    links = PhysicalTable(
        name="order_customer_links",
        columns=(
            PhysicalColumn("link_id", "bigint", nullable=False),
            PhysicalColumn("order_id", "bigint"),
            PhysicalColumn("customer_id", "bigint"),
        ),
        primary_key=("link_id",),
    )
    schema = PhysicalSchema(
        tables=(orders, customers, links),
        foreign_keys=(
            ForeignKey("order_customer_links", ("order_id",), "orders", ("order_id",)),
            ForeignKey(
                "order_customer_links",
                ("customer_id",),
                "customers",
                ("customer_id",),
            ),
        ),
    )
    from tablefold.t2sql.preset import fold_star_schema

    result = fold_star_schema(schema)
    graph = SchemaGraph.build(result.schema)
    # 조합 표 앵커가 실제로 살아남았는지 전제 확인 — 없으면 이 테스트는 공허하다.
    assert any(m.base_table == "order_customer_links" for m in result.layer.models)

    with_graph = str(build_router_prompt("주문 고객 목록", result.layer, graph=graph))
    without = str(build_router_prompt("주문 고객 목록", result.layer))

    assert "조합 표" in with_graph
    # 조합 표는 상세 블록 대신 관계 한 줄로 내려간다.
    compact_line = next(
        ln for ln in with_graph.splitlines() if "order_customer_links =" in ln
    )
    assert {"orders", "customers"} <= set(
        compact_line.split("= ")[1].split(" 조합")[0].split(" × ")
    )
    assert compact_line.endswith("조합")
    link_block = with_graph.split("### order_customer_links", 1)[1]
    assert "한 줄 =" not in link_block.split("\n\n", 1)[0]
    # graph 미전달 시 상세 블록 그대로다 (호출자 호환).
    assert "### order_customer_links" in without
    assert "조합 표" not in without


def test_without_a_graph_the_catalog_keeps_its_old_shape():
    """graph 가 없으면 기존 형태다 — 호출자 호환이 깨지면 안 된다."""
    layer = LogicalLayer(models=(), source_table_count=0, source_column_count=0)
    prompt = build_router_prompt("질문", layer)

    assert "조합 표" not in str(prompt)
