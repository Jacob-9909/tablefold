from __future__ import annotations

import pytest
from tablefold.classify import profile_tables
from tablefold.cluster import SelectionPolicy, cluster
from tablefold.compose import ComposeOptions, compose

from tablefold.presentation import fidelity as fid


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
