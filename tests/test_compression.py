"""컬럼 압축 지표 — 화면의 흐름도와 스트립이 읽는 숫자.

여기서 틀린 숫자는 화면에서 거짓말이 된다. 특히 "물리 컬럼 수"는 모델 사이의
중복을 빼고 세야 한다 — 같은 컬럼이 두 모델에 살아 있으면 1:1 압축이 2:1 처럼
보인다.
"""

from __future__ import annotations

from tablefold.ir import (
    Cardinality,
    FieldKind,
    FieldSource,
    JoinStep,
    LogicalField,
    LogicalLayer,
    LogicalModel,
)
from tablefold.report.compression import measure


def field(
    name,
    table,
    column,
    *,
    kind=FieldKind.BASE,
    aggregate=None,
    path=(),
    filter_only=False,
):
    return LogicalField(
        name=name,
        type="bigint",
        source=FieldSource(
            kind=kind, table=table, column=column, path=path, aggregate=aggregate
        ),
        filter_only=filter_only,
    )


def joined(name, table, column, hops):
    step = JoinStep(
        from_table="f",
        from_columns=("k",),
        to_table=table,
        to_columns=(column,),
        cardinality=Cardinality.MANY_TO_ONE,
    )
    return field(
        name,
        table,
        column,
        kind=FieldKind.JOINED,
        path=tuple([step] * hops),
    )


def layer(*models):
    return LogicalLayer(
        models=models,
        source_table_count=10,
        source_column_count=100,
        covered_table_count=len(models),
    )


def model(name, base, fields):
    return LogicalModel(
        name=name,
        base_table=base,
        fields=fields,
        absorbed_tables=tuple(sorted({f.source.table for f in fields} - {base})),
    )


def test_the_ratio_counts_distinct_pairs_not_field_copies():
    """같은 컬럼이 두 모델에 나뒀다고 두 배로 세면 압축률이 부풀어 오른다."""
    lay = layer(
        model(
            "M1",
            "orders",
            [field("total", "orders", "total"), field("id", "orders", "id")],
        ),
        model(
            "M2",
            "customers",
            [
                field("cust_id", "customers", "id"),
                # orders.total 이 M2 에도 조인으로 들어왔다 — 흔한 모양이다.
                joined("order_total", "orders", "total", 1),
            ],
        ),
    )

    result = measure(lay)

    assert result["physical_columns"] == 3  # (orders,total) (orders,id) (customers,id)


def test_filter_only_fields_are_counted_as_conditions():
    """WHERE 에만 쓰이는 필드를 값으로 세면 꺼낼 수 있는 컬럼이 과대계된다."""
    lay = layer(
        model(
            "M",
            "f",
            [
                field("amt", "f", "amt"),
                field(
                    "day",
                    "child",
                    "day",
                    kind=FieldKind.AGGREGATED,
                    aggregate="sum",
                    filter_only=True,
                ),
            ],
        ),
    )

    by_kind = measure(lay)["models"][0]["by_kind"]

    assert by_kind["filter"] == 1
    assert by_kind["base"] == 1


def test_flows_describe_where_columns_came_from():
    lay = layer(
        model(
            "M",
            "f",
            [
                field("a", "f", "a"),
                field("b", "f", "b"),
                joined("c", "d_org", "c", 1),
                field("n", "child", "x", kind=FieldKind.AGGREGATED, aggregate="sum"),
                field("n2", "child", "y", kind=FieldKind.AGGREGATED, aggregate="sum"),
            ],
        ),
    )

    flows = measure(lay)["models"][0]["flows"]
    by_table = {f["table"]: f for f in flows}

    assert by_table["f"]["role"] == "anchor"
    assert by_table["d_org"]["columns"] == 1
    assert by_table["child"]["columns"] == 2
    assert by_table["child"]["role"] == "aggregated"  # 합계로 접힌 자식


def test_max_hops_and_children_are_reported():
    lay = layer(
        model(
            "M",
            "f",
            [
                field("a", "f", "a"),
                joined("far", "far_dim", "x", 2),
            ],
        ),
    )

    m = measure(lay)["models"][0]

    assert m["max_hops"] == 2
    assert m["source_tables"] == 2
