from __future__ import annotations

import pytest

from tablefold.build.compose import ComposeOptions, compose
from tablefold.choose.classify import profile_tables
from tablefold.choose.cluster import SelectionPolicy, cluster
from tablefold.report import fidelity as fid


def _layer(graph, *, max_areas: int = 2, **compose_kwargs):
    clustering = cluster(
        graph, profile_tables(graph), policy=SelectionPolicy(max_areas=max_areas)
    )
    return compose(graph, clustering, options=ComposeOptions(**compose_kwargs))


@pytest.fixture
def tiny_measure(tiny_graph):
    return fid.measure(_layer(tiny_graph), tiny_graph)


def test_exposed_and_dropped_partition_every_column(tiny_graph, tiny_measure):
    total = sum(len(t.columns) for t in tiny_graph.schema.tables)

    assert tiny_measure.total_columns == total
    assert (
        tiny_measure.exposed_columns
        + tiny_measure.dropped_key_columns
        + tiny_measure.dropped_value_columns
        == total
    )


def test_join_keys_count_as_structural_not_lost(tiny_measure):
    """``orders.customer_id`` 는 필드로 안 나오지만 조인은 그것으로 이뤄진다.

    값 컬럼과 같이 세면 대리키가 많은 스키마가 부당하게 나쁘게 나온다.
    """
    orders = next(t for t in tiny_measure.tables if t.table == "orders")

    assert "customer_id" in orders.dropped_keys
    assert "customer_id" not in orders.dropped_values


def test_absorbed_edge_is_counted_once_per_pair(tiny_measure):
    # tiny 스키마의 FK 3개는 모두 서로 다른 테이블 쌍이다.
    assert tiny_measure.total_edges == 3
    assert 0 < tiny_measure.absorbed_edges <= 3


def test_pair_answerability_matches_model_membership(tiny_graph):
    """한 모델이 두 테이블을 함께 담으면 그 쌍은 답할 수 있어야 한다."""
    layer = _layer(tiny_graph, max_hops=3)
    measured = fid.measure(layer, tiny_graph)

    orders = layer.model("orders")
    members = {orders.base_table.lower(), *(t.lower() for t in orders.absorbed_tables)}

    assert {"customers", "tiers"} <= members
    assert frozenset({"customers", "tiers"}) not in {
        frozenset(p) for p in measured.unanswerable
    }


def test_aggregating_a_child_is_reported_as_guarded(tiny_graph):
    """1:N 자식을 집계로 접으면 팬아웃 방어로 잡힌다. 끄면 0 이어야 한다."""
    with_agg = fid.measure(_layer(tiny_graph, include_aggregates=True), tiny_graph)
    without = fid.measure(_layer(tiny_graph, include_aggregates=False), tiny_graph)

    assert with_agg.fanout_guarded > 0
    assert without.fanout_guarded == 0


def test_dropping_a_table_cannot_improve_column_retention(retail_graph):
    """압축률과 달리 보존율은 테이블을 버려서 좋게 만들 수 없다.

    모델을 하나로 줄이면 분자만 줄고 분모는 그대로이므로 반드시 나빠진다.
    이전의 ``compression_ratio`` 가 정확히 반대로 보상하던 자리다.
    """
    wide = fid.measure(_layer(retail_graph, max_areas=6), retail_graph)
    narrow = fid.measure(_layer(retail_graph, max_areas=1), retail_graph)

    assert narrow.column_retention < wide.column_retention
    assert narrow.pair_answerability < wide.pair_answerability


def test_empty_schema_does_not_divide_by_zero(tiny_graph):
    from tablefold.ir import LogicalLayer

    measured = fid.measure(LogicalLayer(models=()), tiny_graph)

    assert measured.column_retention == 0.0
    assert measured.answerable_pairs == 0
    assert measured.pair_answerability == 0.0


def test_report_and_dict_agree_on_the_headline_numbers(tiny_measure):
    payload = fid.to_dict(tiny_measure)

    assert payload["column_retention"] == round(tiny_measure.column_retention, 4)
    assert payload["counts"]["askable_pairs"] == tiny_measure.askable_pairs
    assert len(payload["tables"]) == len(tiny_measure.tables)


# ── 그룹 가능성 ───────────────────────────────────────────────────────────────


def test_a_table_absorbed_only_as_filters_is_not_groupable(tiny_graph):
    """흡수됐다고 답할 수 있는 것이 아니다.

    1:N 자식으로 흡수된 표는 컬럼이 ``filter_only`` 로만 나온다 — WHERE 는 되고
    SELECT/GROUP BY 는 안 된다. "거래처별", "계정별" 은 GROUP BY 를 요구하므로 그
    표가 그룹 가능하지 않으면 답이 없다.

    ``pair_answerability`` 는 이걸 못 잡는다. 흡수 여부만 보기 때문에 실제 생성이
    60% 인 레이어에서도 100% 를 보고한다.
    """
    layer = _layer(tiny_graph, max_areas=1, expose_child_filters=True)
    measured = fid.measure(layer, tiny_graph)

    absorbed = {
        t.lower()
        for m in layer.models
        for t in (m.base_table, *m.absorbed_tables)
    }
    groupable = set(measured.groupable_tables)

    assert groupable <= absorbed
    assert set(measured.ungroupable) == absorbed - groupable


def test_groupability_counts_joined_columns_as_projectable(tiny_graph):
    """다대일로 인라인된 표는 앵커 한 행에 값이 하나뿐이라 GROUP BY 가 된다."""
    layer = _layer(tiny_graph, max_areas=1, max_hops=3)
    measured = fid.measure(layer, tiny_graph)

    orders = layer.model("orders")
    assert orders is not None
    assert "orders" in measured.groupable_tables
    assert "customers" in measured.groupable_tables


def test_the_report_does_not_call_reachability_answerability(tiny_graph):
    """지표 이름이 재는 것보다 크게 주장하면 안 된다."""
    text = fid.render_report(fid.measure(_layer(tiny_graph), tiny_graph))

    assert "그룹 가능" in text


# ── 예산 절삭 ─────────────────────────────────────────────────────────────────
#
# ``absorbed_tables`` 는 앵커가 도달 *가능한* 표다. 필드 예산에 눌려 그 표의
# 컬럼이 하나도 안 남아도 목록에는 그대로 있다. 구조 지표 셋이 그 목록을 읽는
# 동안 "필드가 하나도 없는 표"가 100% 로 잡혔다.


def test_a_table_trimmed_to_nothing_is_not_counted_as_covered(retail_graph):
    from tablefold.build.compose import ComposeOptions, compose
    from tablefold.choose.classify import profile_tables
    from tablefold.choose.cluster import SelectionPolicy, cluster

    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    generous = compose(
        retail_graph, clustering, options=ComposeOptions(field_budget=10_000)
    )
    starved = compose(
        retail_graph, clustering, options=ComposeOptions(field_budget=30)
    )

    wide = fid.measure(generous, retail_graph)
    thin = fid.measure(starved, retail_graph)

    # 예산을 30필드로 조이면 대부분의 표가 컬럼을 하나도 못 낸다. 커버리지가
    # 그대로면 그 지표는 예산을 못 보고 있는 것이다.
    def covered(report):
        return sum(1 for t in report.tables if t.in_models)

    assert covered(thin) < covered(wide)
    assert thin.pair_answerability < wide.pair_answerability


def test_tables_with_no_field_are_reported_as_not_in_any_model(retail_graph):
    from tablefold.build.compose import ComposeOptions, compose
    from tablefold.choose.classify import profile_tables
    from tablefold.choose.cluster import SelectionPolicy, cluster

    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering, options=ComposeOptions(field_budget=30))
    report = fid.measure(layer, retail_graph)

    for table in report.tables:
        if not table.exposed:
            assert table.in_models == (), table.table


def test_lineage_keeps_two_paths_to_the_same_table_apart():
    """``buyer_id`` 와 ``seller_id`` 가 둘 다 ``users`` 를 가리키는 모양.

    테이블 이름으로 묶으면 두 경로가 한 항목이 되고 ``join_columns`` 가 나중
    것으로 덮인다 — 화면의 ERD 에서 조인 하나가 통째로 사라진다.
    """
    from tablefold.build.compose import ComposeOptions, compose
    from tablefold.choose.classify import profile_tables
    from tablefold.choose.cluster import SelectionPolicy, cluster
    from tablefold.choose.select import ExplicitSelector
    from tablefold.ir import (
        ForeignKey,
        PhysicalColumn,
        PhysicalSchema,
        PhysicalTable,
    )
    from tablefold.relate.graph import SchemaGraph
    from tablefold.report import lineage as lin

    def col(name, type_="bigint"):
        return PhysicalColumn(name=name, type=type_)

    users = PhysicalTable(
        name="users",
        columns=(col("id"), col("name", "varchar(50)")),
        primary_key=("id",),
    )
    orders = PhysicalTable(
        name="orders",
        columns=(col("id"), col("buyer_id"), col("seller_id"), col("total", "float")),
        primary_key=("id",),
    )
    schema = PhysicalSchema(
        tables=(users, orders),
        foreign_keys=(
            ForeignKey("orders", ("buyer_id",), "users", ("id",)),
            ForeignKey("orders", ("seller_id",), "users", ("id",)),
        ),
    )
    graph = SchemaGraph.build(schema)
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector(("orders",)),
    )
    layer = compose(graph, clustering, options=ComposeOptions(max_hops=1))
    report = lin.to_graph(layer, graph)

    sources = report["models"][0]["sources"]
    from_users = [s for s in sources if s["table"] == "users"]

    assert len(from_users) == 2, [s["join_columns"] for s in from_users]
    joins = {tuple(s["join_columns"]) for s in from_users}
    assert len(joins) == 2
