from __future__ import annotations

from dataclasses import replace

import pytest

from tablefold.build.compose import ComposeOptions, compose
from tablefold.choose.classify import profile_tables
from tablefold.choose.cluster import SelectionPolicy, cluster
from tablefold.choose.select import ExplicitSelector
from tablefold.ir import (
    Cardinality,
    FieldKind,
    ForeignKey,
    PhysicalSchema,
    PhysicalTable,
)
from tablefold.relate.graph import SchemaGraph
from tests.conftest import column


@pytest.fixture
def tiny_layer(tiny_graph):
    clustering = cluster(
        tiny_graph, profile_tables(tiny_graph), policy=SelectionPolicy(max_areas=2)
    )
    return compose(tiny_graph, clustering)


def test_model_is_anchored_on_its_base_table(tiny_layer):
    orders = tiny_layer.model("orders")

    assert orders is not None
    assert orders.base_table == "orders"


def test_base_columns_keep_their_names(tiny_layer):
    orders = tiny_layer.model("orders")

    assert orders.field("total").source.kind is FieldKind.BASE
    assert orders.field("placed_at").source.kind is FieldKind.BASE


def test_foreign_key_columns_are_replaced_by_what_they_point_at(tiny_layer):
    orders = tiny_layer.model("orders")

    # The raw key is dropped; the row it identifies is promoted instead.
    assert orders.field("customer_id") is None
    assert orders.field("customer_email") is not None


def test_promoted_columns_are_prefixed_by_their_source(tiny_layer):
    orders = tiny_layer.model("orders")
    email = orders.field("customer_email")

    assert email.source.kind is FieldKind.JOINED
    assert (email.source.table, email.source.column) == ("customers", "email")
    assert email.source.hops == 1


def test_two_hop_columns_carry_the_whole_path(tiny_layer):
    orders = tiny_layer.model("orders")
    label = orders.field("tier_label")

    assert label is not None
    assert label.source.hops == 2
    assert [s.to_table for s in label.source.path] == ["customers", "tiers"]
    assert all(s.cardinality is Cardinality.MANY_TO_ONE for s in label.source.path)


def test_child_columns_are_only_ever_aggregated(tiny_layer):
    """A one-to-many child must not contribute a plain column.

    Inlining one would multiply the anchor's rows, and every sum in a query
    over the model would come back inflated with no error raised.
    """
    orders = tiny_layer.model("orders")

    from_child = [f for f in orders.fields if f.source.table == "order_items"]
    assert from_child
    assert all(f.source.kind is FieldKind.AGGREGATED for f in from_child)
    assert all(f.source.aggregate for f in from_child)
    assert all(
        f.source.path[0].cardinality is Cardinality.ONE_TO_MANY for f in from_child
    )


def test_children_contribute_a_row_count(tiny_layer):
    count = tiny_layer.model("orders").field("order_items_count")

    assert count is not None
    assert count.source.aggregate == "count"
    assert count.source.column == "*"


def test_aggregates_can_be_switched_off(tiny_graph):
    clustering = cluster(
        tiny_graph, profile_tables(tiny_graph), policy=SelectionPolicy(max_areas=2)
    )
    layer = compose(
        tiny_graph, clustering, options=ComposeOptions(include_aggregates=False)
    )

    orders = layer.model("orders")
    assert all(f.source.kind is not FieldKind.AGGREGATED for f in orders.fields)


def test_field_names_are_unique_within_a_model(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering)

    for model in layer.models:
        names = [f.name.lower() for f in model.fields]
        assert len(names) == len(set(names)), model.name


def test_the_budget_is_spent_on_the_layer_not_per_model(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering, options=ComposeOptions(field_budget=60))

    # The budget binds on the sum, not on any single model.
    assert layer.field_count == 60

    # And it is shared: every model got to state its own columns before any
    # model spent budget on a distant join.
    for model in layer.models:
        assert model.fields, f"{model.name} was starved"


def test_base_columns_outrank_everything_under_a_tight_budget(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering, options=ComposeOptions(field_budget=40))

    orders = layer.model("orders")
    base_columns = {c.name for c in retail_graph.schema.table("orders").columns}
    kept_base = {f.name for f in orders.fields if f.source.kind is FieldKind.BASE}
    assert kept_base <= base_columns
    assert len(kept_base) >= 10


def test_no_model_runs_away_with_the_budget(retail_graph):
    """모델당 상한이 하던 일은 라운드로빈이 한다.

    상한은 라운드로빈 이전의 안전장치였고, 실측에서 어떤 값을 줘도 같은 레이어가
    나왔다(NL2SQL 최대 86필드, retail 최대 62필드로 64/200/10000 어디에도 안
    닿는다). 진짜로 독식을 막는 것은 :func:`_allocate` 의 위치 기준 편입이다.
    """
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering, options=ComposeOptions(field_budget=80))

    sizes = sorted((len(m.fields) for m in layer.models), reverse=True)
    assert all(sizes), "굶은 모델이 있으면 라운드로빈이 안 도는 것이다"
    # 가장 큰 모델이 예산의 절반을 넘기지 못한다.
    assert sizes[0] <= sum(sizes) / 2


def test_hop_budget_bounds_how_far_fields_travel(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(
        retail_graph, clustering, options=ComposeOptions(max_hops=1, field_budget=200)
    )

    for model in layer.models:
        assert all(f.source.hops <= 1 for f in model.fields)


def test_the_fold_actually_compresses(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering)

    assert len(layer.models) == 4
    assert layer.source_table_count == 53
    assert layer.source_column_count == 282

    # Columns per field. Unlike tables-per-model this cannot be improved by
    # dropping tables — a discarded table's columns leave the numerator too.
    assert layer.compression_ratio > 1.5


def test_uncovered_tables_are_reported_not_hidden(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering)

    assert layer.notes
    assert all(note.startswith("uncovered: ") for note in layer.notes)


# ── 주석 배선 ─────────────────────────────────────────────────────────────────
#
# 웨어하우스는 컬럼 주석에 컬럼만 설명하고 소속 테이블은 설명하지 않는다.
# ``D_FI_ORG.COMPANY_NM`` 과 ``D_ORG.COMPANY_NM`` 은 주석이 둘 다 "전사명"이라
# 인라인되고 나면 어느 조직 체계인지 알 수 없다. 구분 정보는 테이블 주석
# ("재무조직" / "전사조직")에 있으므로 필드 설명에 함께 실어야 한다.


@pytest.fixture
def commented_graph():
    def commented(name: str, type_: str, comment: str | None):
        return replace(column(name, type_), comment=comment)

    facts = PhysicalTable(
        name="F_BS",
        columns=(
            commented("YYYYMM", "varchar(6)", "기준년월"),
            commented("ORG_CD", "varchar(7)", "조직코드"),
            commented("BS_AMT", "float", "대차금액"),
        ),
        primary_key=("YYYYMM", "ORG_CD"),
        comment="F_대차대조표",
    )
    fi_org = PhysicalTable(
        name="D_FI_ORG",
        columns=(
            commented("ORG_CD", "varchar(7)", "조직코드"),
            commented("HEAD_NM", "varchar(50)", "사업장명"),
        ),
        primary_key=("ORG_CD",),
        comment="D_재무조직",
    )
    schema = PhysicalSchema(
        tables=(facts, fi_org),
        foreign_keys=(
            ForeignKey(
                from_table="F_BS",
                from_columns=("ORG_CD",),
                to_table="D_FI_ORG",
                to_columns=("ORG_CD",),
            ),
        ),
    )
    return SchemaGraph.build(schema)


def _fold_one(graph, anchor: str):
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector((anchor,)),
    )
    return compose(graph, clustering, options=ComposeOptions(max_hops=2))


def test_joined_field_description_names_its_source_table(commented_graph):
    """인라인된 컬럼은 어느 테이블에서 왔는지 설명에 담아야 한다."""
    model = _fold_one(commented_graph, "F_BS").models[0]
    head = next(f for f in model.fields if f.source.column == "HEAD_NM")

    assert head.description is not None
    assert "사업장명" in head.description
    assert "재무조직" in head.description


def test_base_field_description_stays_bare(commented_graph):
    """앵커 자신의 컬럼은 출처가 자명하다. 이름을 덧붙이면 잡음만 는다."""
    model = _fold_one(commented_graph, "F_BS").models[0]
    amt = next(f for f in model.fields if f.source.column == "BS_AMT")

    assert amt.description == "대차금액"


def test_model_description_uses_the_table_comment(commented_graph):
    """``F_BS`` 를 영어 복수형으로 보고 ``f_b`` 로 자르면 안 된다."""
    model = _fold_one(commented_graph, "F_BS").models[0]

    assert "F_대차대조표" in model.description
    assert "f_b" not in model.description


def test_parenthesised_table_comment_is_not_double_wrapped(commented_graph):
    """실측 스키마의 ``D_ORG`` 주석은 ``(조직코드)`` 다. 그대로 실으면 괄호가 겹친다."""
    schema = commented_graph.schema
    tables = tuple(
        replace(t, comment="(조직코드)") if t.name == "D_FI_ORG" else t
        for t in schema.tables
    )
    graph = SchemaGraph.build(
        PhysicalSchema(tables=tables, foreign_keys=schema.foreign_keys)
    )
    model = _fold_one(graph, "F_BS").models[0]
    head = next(f for f in model.fields if f.source.column == "HEAD_NM")

    assert head.description == "사업장명 (조직코드)"


def test_uncommented_column_keeps_the_label_in_parentheses(commented_graph):
    """주석 없는 컬럼에 라벨만 남기면 라벨이 컬럼의 뜻처럼 읽힌다."""
    schema = commented_graph.schema
    tables = tuple(
        replace(
            t,
            columns=tuple(
                replace(c, comment=None) if c.name == "HEAD_NM" else c
                for c in t.columns
            ),
        )
        if t.name == "D_FI_ORG"
        else t
        for t in schema.tables
    )
    graph = SchemaGraph.build(
        PhysicalSchema(tables=tables, foreign_keys=schema.foreign_keys)
    )
    model = _fold_one(graph, "F_BS").models[0]
    head = next(f for f in model.fields if f.source.column == "HEAD_NM")

    assert head.description == "(D_재무조직)"


def test_a_dimension_anchor_does_not_filter_on_itself():
    """앵커가 차원일 때 자식이 앵커를 되가리키면 자기 참조 필터가 생긴다.

    ``D_ORG`` 앵커에 ``F_SALES`` 가 자식으로 붙고, ``F_SALES`` 는 ``D_ORG`` 를
    참조한다. 그 통로를 그대로 만들면 ``f_sales_HEAD_NM`` 같은 필드가 나오는데,
    앵커에 이미 ``HEAD_NM`` 이 있으므로 조건을 걸 새 자리가 아니다. 실측에서
    ``D_ORG`` 모델 145필드 중 12개가 이것이었다.
    """
    org = PhysicalTable(
        name="D_ORG",
        columns=(column("ORG_CD"), column("HEAD_NM", "varchar(50)")),
        primary_key=("ORG_CD",),
    )
    sales = PhysicalTable(
        name="F_SALES",
        columns=(
            column("ORG_CD"),
            column("YYYYMM", "varchar(6)"),
            column("SALES_AMT", "float"),
        ),
    )
    schema = PhysicalSchema(
        tables=(org, sales),
        foreign_keys=(
            ForeignKey(
                from_table="F_SALES",
                from_columns=("ORG_CD",),
                to_table="D_ORG",
                to_columns=("ORG_CD",),
            ),
        ),
    )
    graph = SchemaGraph.build(schema)
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector(("D_ORG",)),
    )
    layer = compose(
        graph, clustering, options=ComposeOptions(expose_child_filters=True)
    )
    model = layer.models[0]

    # 자식을 거쳐 앵커 자신으로 되돌아온 필터 필드가 있으면 안 된다.
    loops = [
        f
        for f in model.fields
        if f.filter_only and f.source.table.lower() == "d_org"
    ]
    assert loops == []
    # 자식 자신의 컬럼으로 거는 통로는 남아 있어야 한다.
    assert any(f.filter_only and f.source.table == "F_SALES" for f in model.fields)


def test_a_third_colliding_name_is_qualified_not_dropped(commented_graph):
    """이름이 두 번 충돌하면 ``_deduplicate`` 가 필드를 조용히 버렸다.

    실측에서 조직 차원이 셋(``D_FI_ORG`` · ``D_ORG`` · ``D_SA_ORG``)이고 전부
    ``ORG_CD`` 로 닿는다. 역할 이름이 셋 다 ``org`` 로 같아서 두 번째까지만
    살아남고 세 번째 차원의 ``COMPANY_NM`` 은 레이어에서 사라졌다 — 버려졌다는
    기록도 남지 않는다.
    """
    schema = commented_graph.schema
    extra = PhysicalTable(
        name="D_SA_ORG",
        columns=(
            replace(column("ORG_CD", "varchar(7)"), comment="조직코드"),
            replace(column("HEAD_NM", "varchar(50)"), comment="사업장명"),
        ),
        primary_key=("ORG_CD",),
        comment="D_영업조직",
    )
    graph = SchemaGraph.build(
        PhysicalSchema(
            tables=(*schema.tables, extra),
            foreign_keys=(
                *schema.foreign_keys,
                ForeignKey(
                    from_table="F_BS",
                    from_columns=("ORG_CD",),
                    to_table="D_SA_ORG",
                    to_columns=("ORG_CD",),
                ),
            ),
        )
    )
    model = _fold_one(graph, "F_BS").models[0]
    sources = {f.source.table for f in model.fields if f.source.column == "HEAD_NM"}

    assert sources == {"D_FI_ORG", "D_SA_ORG"}
    names = [f.name.lower() for f in model.fields]
    assert len(names) == len(set(names))


# ── 문자 예산 ─────────────────────────────────────────────────────────────────
#
# 진짜 제약은 프롬프트 길이지 필드 수가 아니다. 필드 수는 대리 변수이고,
# 실측에서 필드당 평균 50자였지만 이름·주석 길이에 따라 흔들린다.


@pytest.mark.parametrize("budget", [3_000, 6_000, 12_000])
def test_prompt_budget_is_honoured_by_the_rendered_text(retail_graph, budget):
    from tablefold.report.prompt import render_text

    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(
        retail_graph, clustering, options=ComposeOptions(prompt_budget=budget)
    )

    assert len(render_text(layer)) <= budget


def test_a_bigger_prompt_budget_never_loses_fields(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    small = compose(
        retail_graph, clustering, options=ComposeOptions(prompt_budget=4_000)
    )
    large = compose(
        retail_graph, clustering, options=ComposeOptions(prompt_budget=12_000)
    )

    assert large.field_count > small.field_count


def test_field_budget_still_works_when_no_prompt_budget_is_given(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering, options=ComposeOptions(field_budget=60))

    assert layer.field_count == 60
