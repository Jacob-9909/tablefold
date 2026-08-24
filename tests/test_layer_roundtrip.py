"""레이어 되읽기.

``expand --layer`` 가 승인된 레이어를 재사용하려면 :func:`to_dict` 의 역이
있어야 한다. 없는 동안 CLI 는 ``--layer`` 를 받아 놓고 경고만 찍은 뒤 스키마에서
다시 접었다 — 설정이 조금만 달라도 승인한 것과 다른 계약을 상대로 확장한다.
"""

from __future__ import annotations

import pytest

from tablefold.build.compose import ComposeOptions, compose
from tablefold.choose.classify import profile_tables
from tablefold.choose.cluster import SelectionPolicy, cluster
from tablefold.report import prompt as emit
from tablefold.rewrite.expand import expand


@pytest.fixture
def layer(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    return compose(
        retail_graph, clustering, options=ComposeOptions(expose_child_filters=True)
    )


def test_json_round_trip_preserves_the_layer(layer):
    again = emit.from_json(emit.to_json(layer))

    assert emit.to_dict(again) == emit.to_dict(layer)


def test_yaml_round_trip_preserves_the_layer(layer):
    again = emit.from_yaml(emit.to_yaml(layer))

    assert emit.to_dict(again) == emit.to_dict(layer)


def test_load_accepts_either_format(layer):
    assert emit.to_dict(emit.load(emit.to_json(layer))) == emit.to_dict(layer)
    assert emit.to_dict(emit.load(emit.to_yaml(layer))) == emit.to_dict(layer)


def test_a_reloaded_layer_expands_the_same(layer, retail_graph):
    model = next(m for m in layer.models if len(m.fields) > 2)
    field = next(f for f in model.fields if not f.filter_only)
    sql = f"SELECT {field.name} FROM {model.name}"

    original = expand(sql, layer, retail_graph).sql
    reloaded = expand(sql, emit.from_json(emit.to_json(layer)), retail_graph).sql

    assert reloaded == original


def test_derived_keys_survive_the_round_trip():
    """파생키가 빠지면 되읽은 레이어가 등가 조인을 만든다 — 값이 조용히 틀린다."""
    from tablefold.choose.select import ExplicitSelector
    from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable
    from tablefold.relate.graph import SchemaGraph

    def col(name, type_="varchar(8)"):
        return PhysicalColumn(name=name, type=type_)

    sales = PhysicalTable(
        name="F_SALES", columns=(col("YYYYMMDD"), col("AMT", "float"))
    )
    cal = PhysicalTable(
        name="D_CAL",
        columns=(col("YYYYMM", "varchar(6)"), col("YYYY", "varchar(4)")),
        primary_key=("YYYYMM",),
    )
    schema = PhysicalSchema(
        tables=(sales, cal),
        foreign_keys=(
            ForeignKey(
                from_table="F_SALES",
                from_columns=("YYYYMMDD",),
                to_table="D_CAL",
                to_columns=("YYYYMM",),
                key_expressions=("SUBSTRING(YYYYMMDD, 1, 6)",),
            ),
        ),
    )
    graph = SchemaGraph.build(schema)
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector(("F_SALES",)),
    )
    built = compose(graph, clustering, options=ComposeOptions(max_hops=1))

    again = emit.from_json(emit.to_json(built))
    steps = [s for m in again.models for f in m.fields for s in f.source.path]

    assert steps
    assert any(s.key_expressions == ("SUBSTRING(YYYYMMDD, 1, 6)",) for s in steps)

    sql = expand(
        "SELECT d_cal_YYYY, AMT FROM F_SALES", again, graph, dialect="sqlite"
    ).sql
    assert "SUBSTR" in sql.upper()


def test_a_layer_from_the_future_is_refused():
    with pytest.raises(ValueError, match="version"):
        emit.from_dict({"version": 99, "models": []})
