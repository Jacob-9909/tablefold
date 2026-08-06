"""파생키 조인과 가상 앵커.

FK 엣지는 컬럼 등가로만 표현된다. 실측 웨어하우스에는 그것으로 못 잇는 관계가
둘 있다:

* **파생키** — ``F_SALES.YYYYMMDD`` 와 ``V_CALENDAR.YYYYMM`` 은 같은 기간을
  뜻하지만 값이 다르다. ``substr(YYYYMMDD, 1, 6) = YYYYMM`` 이라야 이어진다.
* **차원 테이블 부재** — 실측 스키마에서 ``YYYYMM`` 은 7개 팩트에 있는데
  ``D_CALENDAR`` 가 없다. 앵커로 삼을 테이블이 존재하지 않으므로 기간 질문이
  ``filter_only`` 통로로만 가능하고, 팩트 간 기간 비교는 아예 불가능하다.

둘 다 **줄 수를 바꾸지 않는다** — 파생키는 여전히 N:1 등가이고, 가상 앵커는
키당 정확히 한 행이다. 입도 보호는 그대로 성립한다.
"""

from __future__ import annotations

import sqlite3

import pytest

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
from tablefold.rewrite.expand import ExpansionError, expand


def _col(name: str, type_: str = "varchar(8)") -> PhysicalColumn:
    return PhysicalColumn(name=name, type=type_)


CALENDAR_SQL = (
    "SELECT YYYYMM, substr(YYYYMM, 1, 4) AS YYYY FROM ("
    "  SELECT YYYYMM FROM F_PL GROUP BY YYYYMM"
    "  UNION SELECT substr(YYYYMMDD, 1, 6) FROM F_SALES"
    "   GROUP BY substr(YYYYMMDD, 1, 6)"
    ") AS periods"
)


@pytest.fixture
def period_schema() -> PhysicalSchema:
    """월 팩트 하나, 일 팩트 하나, 그리고 둘을 잇는 가상 캘린더."""
    pl = PhysicalTable(
        name="F_PL",
        columns=(_col("YYYYMM", "varchar(6)"), _col("PL_AMT", "float")),
    )
    sales = PhysicalTable(
        name="F_SALES",
        columns=(_col("YYYYMMDD", "varchar(8)"), _col("SALES_AMT", "float")),
    )
    calendar = PhysicalTable(
        name="V_CALENDAR",
        columns=(_col("YYYYMM", "varchar(6)"), _col("YYYY", "varchar(4)")),
        primary_key=("YYYYMM",),
        source_sql=CALENDAR_SQL,
        comment="가상 기간 차원",
    )
    return PhysicalSchema(
        tables=(pl, sales, calendar),
        foreign_keys=(
            ForeignKey(
                from_table="F_PL",
                from_columns=("YYYYMM",),
                to_table="V_CALENDAR",
                to_columns=("YYYYMM",),
            ),
            ForeignKey(
                from_table="F_SALES",
                from_columns=("YYYYMMDD",),
                to_table="V_CALENDAR",
                to_columns=("YYYYMM",),
                key_expressions=("substr(YYYYMMDD, 1, 6)",),
            ),
        ),
    )


@pytest.fixture
def period_layer(period_schema):
    graph = SchemaGraph.build(period_schema)
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector(("V_CALENDAR",)),
    )
    return graph, compose(
        graph,
        clustering,
        options=ComposeOptions(max_hops=2, expose_child_filters=True),
    )


# ── 파생키 ────────────────────────────────────────────────────────────────────


def test_derived_key_wraps_the_many_side_before_comparing(period_schema):
    """``key_expressions`` 가 있으면 다 쪽에 식을 씌운 뒤 등가로 맞춘다."""
    graph = SchemaGraph.build(period_schema)
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector(("F_SALES",)),
    )
    layer = compose(graph, clustering, options=ComposeOptions(max_hops=1))

    sql = expand(
        "SELECT v_calendar_YYYY, SALES_AMT FROM F_SALES", layer, graph, dialect="sqlite"
    ).sql

    assert "SUBSTR" in sql.upper()


def test_derived_key_columns_are_qualified_by_the_anchor_alias(period_schema):
    graph = SchemaGraph.build(period_schema)
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector(("F_SALES",)),
    )
    layer = compose(graph, clustering, options=ComposeOptions(max_hops=1))

    sql = expand(
        "SELECT v_calendar_YYYY, SALES_AMT FROM F_SALES", layer, graph, dialect="sqlite"
    ).sql

    # 식 안의 컬럼은 앵커의 베이스 별칭으로 한정되어야 한다.
    assert "base.YYYYMMDD" in sql
    assert "j_v_calendar" in sql.lower()


# ── 가상 테이블 ───────────────────────────────────────────────────────────────


def test_virtual_table_expands_to_its_defining_sql(period_layer):
    graph, layer = period_layer

    sql = expand(
        "SELECT YYYYMM, SUM(f_pls_PL_AMT_sum) FROM V_CALENDAR GROUP BY YYYYMM",
        layer,
        graph,
        dialect="sqlite",
    ).sql

    # 가상 앵커는 물리 테이블이 아니다. 이름을 그대로 내보내면 데이터베이스에서
    # "no such table" 로 터진다.
    assert "UNION" in sql.upper()
    assert "F_PL" in sql.upper()


def test_virtual_anchor_keeps_the_grain(period_layer):
    """가상 앵커는 키당 한 행이라 자식 집계가 부풀지 않는다."""
    graph, layer = period_layer
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE F_PL (YYYYMM TEXT, PL_AMT REAL);
        CREATE TABLE F_SALES (YYYYMMDD TEXT, SALES_AMT REAL);
        INSERT INTO F_PL VALUES ('202507', 100), ('202507', 200), ('202508', 50);
        INSERT INTO F_SALES VALUES
            ('20250703', 10), ('20250715', 20), ('20250801', 5);
        """
    )

    sql = expand(
        "SELECT YYYYMM, SUM(f_pls_PL_AMT_sum) AS pl, "
        "SUM(f_sales_SALES_AMT_sum) AS sales "
        "FROM V_CALENDAR GROUP BY YYYYMM",
        layer,
        graph,
        dialect="sqlite",
    ).sql

    rows = dict((r[0], (r[1], r[2])) for r in conn.execute(sql))

    # 두 팩트를 나란히 붙여도 서로를 복제하지 않는다.
    assert rows["202507"] == (300, 30)
    assert rows["202508"] == (50, 5)


# ── 비등가 (condition) ────────────────────────────────────────────────────────


def _scd2_schema(*, anchor_is_fact: bool) -> PhysicalSchema:
    """근태와 발령 이력. 사번 등가로만 이으면 이력 행 수만큼 근태가 복제된다."""
    attendance = PhysicalTable(
        name="F_ATTENDANCE",
        columns=(
            _col("EMP_NO", "varchar(8)"),
            _col("WORK_YMD", "varchar(8)"),
            _col("WORK_HOURS", "float"),
        ),
    )
    assignment = PhysicalTable(
        name="H_ASSIGNMENT",
        columns=(
            _col("EMP_NO", "varchar(8)"),
            _col("DEPT_NM", "varchar(50)"),
            _col("VALID_FROM", "varchar(8)"),
            _col("VALID_TO", "varchar(8)"),
        ),
        primary_key=("EMP_NO",) if anchor_is_fact else (),
    )
    return PhysicalSchema(
        tables=(attendance, assignment),
        foreign_keys=(
            ForeignKey(
                from_table="F_ATTENDANCE",
                from_columns=("EMP_NO",),
                to_table="H_ASSIGNMENT",
                to_columns=("EMP_NO",),
                condition=(
                    "{L}.EMP_NO = {R}.EMP_NO AND "
                    "{L}.WORK_YMD BETWEEN {R}.VALID_FROM AND {R}.VALID_TO"
                ),
            ),
        ),
    )


def test_non_equi_condition_is_used_when_inlining():
    """N:1 인라인은 임의 술어를 그대로 쓴다. 줄 수가 안 늘기 때문이다."""
    graph = SchemaGraph.build(_scd2_schema(anchor_is_fact=True))
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector(("F_ATTENDANCE",)),
    )
    layer = compose(graph, clustering, options=ComposeOptions(max_hops=1))

    sql = expand(
        "SELECT emp_DEPT_NM, WORK_HOURS FROM F_ATTENDANCE",
        layer,
        graph,
        dialect="sqlite",
    ).sql

    assert "BETWEEN" in sql.upper()
    assert "{L}" not in sql and "{R}" not in sql
    # 왼쪽은 앵커, 오른쪽은 경로 별칭으로 바인딩되어야 한다.
    assert "base.WORK_YMD" in sql
    assert "j_h_assignment" in sql.lower()


def test_non_equi_child_is_refused_rather_than_silently_wrong():
    """1:N 은 자식에서 부모 키를 계산할 수 없다. 접으면 값이 조용히 틀린다."""
    graph = SchemaGraph.build(_scd2_schema(anchor_is_fact=True))
    clustering = cluster(
        graph,
        profile_tables(graph),
        policy=SelectionPolicy(max_areas=1),
        selector=ExplicitSelector(("H_ASSIGNMENT",)),
    )
    layer = compose(
        graph,
        clustering,
        options=ComposeOptions(max_hops=1, expose_child_filters=True),
    )

    with pytest.raises(ExpansionError, match="non-equi"):
        expand(
            "SELECT DEPT_NM, SUM(f_attendances_WORK_HOURS_sum) "
            "FROM H_ASSIGNMENT GROUP BY DEPT_NM",
            layer,
            graph,
            dialect="sqlite",
        )
