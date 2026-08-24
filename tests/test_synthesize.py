"""없는 차원 테이블을 키에서 세운다.

실측 스키마(NL2SQL 19테이블)에 ``YYYYMM`` 이 7개 팩트, ``YYYYMMDD`` 가 3개
팩트에 있는데 캘린더 차원 테이블이 없다. 앵커로 삼을 테이블이 존재하지 않으므로
기간은 ``filter_only`` 통로로만 다뤄지고, "월별 매출 대 계획" 같은 팩트 간 기간
질문은 원리적으로 불가능하다 — 그런 질문을 받을 앵커가 없기 때문이다.

키는 있는데 그 키를 소유한 테이블이 없는 상황이므로, 키를 모아 테이블을 세운다.
"""

from __future__ import annotations

import pytest

from tablefold.ir import PhysicalColumn, PhysicalSchema, PhysicalTable
from tablefold.relate.synthesize import PERIOD_ANCHOR, add_period_anchor


def _col(name: str, type_: str) -> PhysicalColumn:
    return PhysicalColumn(name=name, type=type_)


@pytest.fixture
def facts() -> PhysicalSchema:
    return PhysicalSchema(
        tables=(
            PhysicalTable(
                name="F_PL",
                columns=(_col("YYYYMM", "varchar(6)"), _col("PL_AMT", "float")),
            ),
            PhysicalTable(
                name="F_SALES",
                columns=(
                    _col("YYYYMMDD", "varchar(8)"),
                    _col("SALES_AMT", "float"),
                ),
            ),
            PhysicalTable(
                name="D_ITEM",
                columns=(_col("ITEM_CD", "varchar(8)"),),
                primary_key=("ITEM_CD",),
            ),
        )
    )


def test_period_anchor_is_added_when_no_table_owns_the_key(facts):
    out = add_period_anchor(facts)
    anchor = out.table(PERIOD_ANCHOR)

    assert anchor is not None
    assert anchor.is_virtual
    assert anchor.primary_key == ("YYYYMM",)


def test_month_facts_join_by_equality_and_day_facts_by_expression(facts):
    out = add_period_anchor(facts)
    edges = {fk.from_table: fk for fk in out.foreign_keys}

    assert edges["F_PL"].key_expressions is None
    assert edges["F_SALES"].key_expressions == ("SUBSTRING(YYYYMMDD, 1, 6)",)
    assert all(fk.to_table == PERIOD_ANCHOR for fk in out.foreign_keys)


def test_the_anchor_yields_one_row_per_period(facts):
    """``UNION ALL`` 이면 앵커 자신이 복제되어 입도 보호가 무너진다."""
    sql = add_period_anchor(facts).table(PERIOD_ANCHOR).source_sql

    assert "UNION" in sql.upper()
    assert "UNION ALL" not in sql.upper()


def test_tables_without_a_period_column_are_left_alone(facts):
    out = add_period_anchor(facts)

    assert "D_ITEM" not in {fk.from_table for fk in out.foreign_keys}


def test_nothing_is_added_when_a_real_calendar_already_exists(facts):
    calendar = PhysicalTable(
        name="D_CALENDAR",
        columns=(_col("YYYYMM", "varchar(6)"),),
        primary_key=("YYYYMM",),
    )
    schema = PhysicalSchema(tables=(*facts.tables, calendar))

    assert add_period_anchor(schema) is schema


def test_a_lone_period_column_is_not_worth_an_anchor(facts):
    """한 테이블에만 있으면 앵커가 이어 줄 것이 없다."""
    schema = PhysicalSchema(tables=(facts.tables[0], facts.tables[2]))

    assert add_period_anchor(schema) is schema


def test_length_eight_codes_are_not_mistaken_for_dates():
    """``BS_ACCT_CD varchar(8)`` 은 기간이 아니다. 길이만 보면 안 된다."""
    schema = PhysicalSchema(
        tables=(
            PhysicalTable(name="F_BS", columns=(_col("BS_ACCT_CD", "varchar(8)"),)),
            PhysicalTable(
                name="D_BS_ACCT",
                columns=(_col("BS_ACCT_CD", "varchar(8)"),),
                primary_key=("BS_ACCT_CD",),
            ),
        )
    )

    assert add_period_anchor(schema) is schema


def test_the_synthesized_anchor_survives_redundancy_pruning(facts):
    """중복 판정은 흡수 *조합* 만 본다. 입도가 다른 앵커를 같은 것으로 본다.

    실측에서 ``V_PERIOD`` (월)와 ``D_ORG`` (조직)가 같은 팩트 10개를 담는다는
    이유로 월 앵커가 잘렸다. "조직별 매출 대 계획"은 "월별 매출 대 계획"을
    대신하지 못한다.
    """
    from tablefold.choose.classify import profile_tables
    from tablefold.choose.cluster import SelectionPolicy, cluster
    from tablefold.choose.select import ExplicitSelector
    from tablefold.relate.graph import SchemaGraph

    schema = add_period_anchor(facts)
    graph = SchemaGraph.build(schema)
    anchors = ("F_PL", "F_SALES", PERIOD_ANCHOR)
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=len(anchors)),
        selector=ExplicitSelector(anchors, prune_redundant=True),
    )

    assert PERIOD_ANCHOR in {area.anchor for area in clustering.areas}
