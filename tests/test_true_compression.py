"""정보량 측정 · 동치 컬럼 통합 · 월별 요약 — "진짜 압축" 삼인조.

공통 전제: 판정은 데이터로 한다. 이름이나 추측으로 줄인 것은 압축이 아니라
삭제다. sqlite 로 실측한다.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from tablefold.ir import (
    FieldKind,
    FieldSource,
    ForeignKey,
    LogicalField,
    LogicalLayer,
    LogicalModel,
    PhysicalColumn,
    PhysicalSchema,
    PhysicalTable,
)
from tablefold.relate.equivalence import (
    dedupe_fields,
    find_equivalents,
)
from tablefold.relate.synthesize import add_monthly_summaries
from tablefold.report.information import measure


def col(name, type_="bigint"):
    return PhysicalColumn(name=name, type=type_)


# ── 정보량 ───────────────────────────────────────────────────────────────────


def _layer_with(pairs):
    fields = [
        LogicalField(
            name=colname,
            type="bigint",
            source=FieldSource(kind=FieldKind.BASE, table=table, column=colname),
        )
        for table, colname in pairs
    ]
    model = LogicalModel(name="M", base_table=pairs[0][0], fields=tuple(fields))
    return LogicalLayer(
        models=(model,),
        source_table_count=1,
        source_column_count=len(pairs),
    )


def test_bits_are_log2_of_distinct_values():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE t (A INTEGER, B INTEGER, C INTEGER);
        INSERT INTO t VALUES (1,10,7),(2,20,7),(3,30,7);
        """
    )
    schema = PhysicalSchema(
        tables=(
            PhysicalTable(
                name="t",
                columns=(col("A"), col("B"), col("C")),
            ),
        ),
    )
    lay = _layer_with([("t", "A"), ("t", "B")])

    result = measure(lay, schema, cursor=conn.cursor(), dialect="tsql")

    # A: 3값, B: 3값, C: 1값(상수). 각 항목에 +1 칸.
    expected_source = math.log2(3) + 1 + math.log2(3) + 1 + math.log2(1) + 1
    assert result["measured"] is True
    assert result["source_bits"] == pytest.approx(expected_source)
    assert result["exposed_bits"] == pytest.approx(math.log2(3) * 2 + 2)
    assert result["retention"] == pytest.approx(
        round(result["exposed_bits"] / result["source_bits"], 4)
    )
    conn.close()


def test_without_cursor_only_structure_stats_survive():
    """카디널리티를 모르면서 비트를 지어내면 이 모듈의 존재 이유가 사라진다."""
    lay = _layer_with([("t", "A"), ("t", "B"), ("t", "A")])
    schema = PhysicalSchema(
        tables=(
            PhysicalTable(
                name="t",
                columns=(col("A"), col("B")),
            ),
        )
    )

    result = measure(lay, schema)

    assert result["measured"] is False
    assert result["source_bits"] is None
    # 복제율은 구조만으로 계산된다: 필드 3개, 서로 다른 짝 2개.
    assert result["duplication_factor"] == 1.5


# ── 동치 컬럼 ────────────────────────────────────────────────────────────────


def test_mutually_dependent_columns_merge_and_the_rest_survives():
    """ORG_CD ↔ ORG_ID 는 하나로 접히고, 이름·값 컬럼은 건드리지 않는다."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE D_ORG (
            ORG_CD TEXT PRIMARY KEY,
            ORG_ID INTEGER,
            ORG_NM TEXT,
            HEAD_NM TEXT,
            AMT NUMERIC
        );
        """
    )
    rows = []
    for i in range(1, 11):
        # ORG_CD ↔ ORG_ID 는 항상 같이 결정된다. ORG_NM 은 코드를 넘어
        # 겹치고(두 코드가 같은 이름), HEAD_NM 은 독립적이다.
        rows.append((f"O{i}", 100 + i, f"구역{i % 3}", f"임원{i}", 100 * i))
    conn.executemany("INSERT INTO D_ORG VALUES (?,?,?,?,?)", rows)
    org = PhysicalTable(
        name="D_ORG",
        columns=(
            col("ORG_CD", "varchar"),
            col("ORG_ID"),
            col("ORG_NM", "varchar"),
            col("HEAD_NM", "varchar"),
            col("AMT", "numeric"),
        ),
        primary_key=("ORG_CD",),
    )
    schema = PhysicalSchema(
        tables=(org,),
    )

    groups = find_equivalents(schema, conn.cursor(), dialect="tsql")

    assert len(groups) == 1
    pair = {groups[0].columns[0], groups[0].columns[1]}
    assert len(groups[0].columns) == 2  # 연쇄로 다른 컬럼까지 삼키지 않는다
    assert pair == {"ORG_CD", "ORG_ID"}
    conn.close()


def test_tiny_tables_are_never_merged():
    """두 행짜리 표에서 임의의 두 컬럼은 항상 서로를 결정한다 — 근거 없는 압축."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE TINY (A TEXT, B TEXT, C TEXT);
        INSERT INTO TINY VALUES ('x','p',1),('y','q',2);
        """
    )
    tiny = PhysicalTable(
        name="TINY",
        columns=(col("A", "varchar"), col("B", "varchar"), col("C")),
    )
    schema = PhysicalSchema(tables=(tiny,))

    assert find_equivalents(schema, conn.cursor(), dialect="tsql") == ()
    conn.close()


def test_dedupe_removes_aliases_but_keeps_a_hint():
    rep_field = LogicalField(
        name="ORG_CD",
        type="varchar",
        source=FieldSource(kind=FieldKind.JOINED, table="D_ORG", column="ORG_CD"),
    )
    alias_field = LogicalField(
        name="org_id",
        type="bigint",
        source=FieldSource(kind=FieldKind.JOINED, table="D_ORG", column="ORG_ID"),
    )
    other = LogicalField(
        name="AMT",
        type="numeric",
        source=FieldSource(
            kind=FieldKind.AGGREGATED, table="D_ORG", column="AMT", aggregate="sum"
        ),
    )
    layer = LogicalLayer(
        models=(
            LogicalModel(
                name="M", base_table="D_ORG", fields=(rep_field, alias_field, other)
            ),
        ),
        notes=(),
    )

    from tablefold.relate.equivalence import EquivalentGroup

    new_layer, removed = dedupe_fields(
        layer, (EquivalentGroup(table="D_ORG", columns=("ORG_CD", "ORG_ID")),)
    )

    assert removed == 1
    model = new_layer.models[0]
    names = [f.name for f in model.fields]
    assert names == ["ORG_CD", "AMT"]
    assert "org_id" in (model.fields[0].description or "")


def test_one_way_dependency_is_not_merged():
    """폭이 다른 컬럼끼리는 시험조차 하지 않는다. PK → 속성은 정합 설계다."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE T (
            CODE TEXT PRIMARY KEY,
            LABEL TEXT,
            EXTRA TEXT
        );
        """
    )
    rows = [(f"C{i}", f"라벨{i}", f"기타{i % 2}") for i in range(10)]
    conn.executemany("INSERT INTO T VALUES (?,?,?)", rows)
    t = PhysicalTable(
        name="T",
        columns=(
            col("CODE", "varchar"),
            col("LABEL", "varchar"),
            col("EXTRA", "varchar"),
        ),
        primary_key=("CODE",),
    )

    groups = find_equivalents(
        PhysicalSchema(tables=(t,)), conn.cursor(), dialect="tsql"
    )

    assert groups == ()
    conn.close()


# ── 월별 요약 ────────────────────────────────────────────────────────────────


def test_monthly_summary_groups_by_month_and_carries_dimensions():
    fact = PhysicalTable(
        name="F_SALES",
        columns=(
            col("ID"),
            col("YYYYMMDD", "varchar(8)"),
            col("ORG_CD", "varchar"),
            col("AMT", "numeric"),
        ),
        primary_key=("ID",),
    )
    dim = PhysicalTable(
        name="D_ORG",
        columns=(col("ORG_CD", "varchar"),),
        primary_key=("ORG_CD",),
    )
    schema = PhysicalSchema(
        tables=(fact, dim),
        foreign_keys=(ForeignKey("F_SALES", ("ORG_CD",), "D_ORG", ("ORG_CD",)),),
    )

    enriched, built = add_monthly_summaries(schema)

    assert len(built) == 1
    summary = enriched.table("V_F_SALES_MON")
    assert summary.is_virtual
    # SELECT 와 GROUP BY 가 같은 식이어야 요약이 월 입도로 나온다.
    assert "GROUP BY SUBSTRING(YYYYMMDD, 1, 6), ORG_CD" in summary.source_sql
    assert "SUBSTRING(YYYYMMDD, 1, 6) AS YYYYMM" in summary.source_sql
    assert "SUM(AMT)" in summary.source_sql
    assert "COUNT(*) AS ROW_CNT" in summary.source_sql
    # 요약도 같은 차원에 붙는다 — 조직별 월 매출이 요약에서 바로 답힌다.
    edges_to_dim = [
        fk for fk in enriched.foreign_keys if fk.from_table == "V_F_SALES_MON"
    ]
    assert any(fk.to_table == "D_ORG" for fk in edges_to_dim)


def test_a_fact_without_any_period_key_is_skipped():
    fact = PhysicalTable(
        name="F_THINGS",
        columns=(col("ID"), col("WIDGET", "varchar"), col("N")),
    )
    schema = PhysicalSchema(tables=(fact,), foreign_keys=())

    _, built = add_monthly_summaries(schema)

    assert built == ()


def test_information_measures_through_a_single_level_virtual():
    """물리 팩트 위의 요약 가상 표는 카디널리티를 잴 수 있다."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE D_CUST (CUST_CD TEXT PRIMARY KEY);
        CREATE TABLE F_SALES (
            ID INTEGER PRIMARY KEY, YYYYMMDD TEXT, CUST_CD TEXT, AMT NUMERIC);
        INSERT INTO D_CUST VALUES ('C1'),('C2');
        INSERT INTO F_SALES VALUES
            (1,'20250601','C1',100),(2,'20250615','C1',200),
            (3,'20250701','C2',300),(4,'20250730','C2',400);
        """
    )
    dim = PhysicalTable(
        name="D_CUST", columns=(col("CUST_CD", "varchar"),), primary_key=("CUST_CD",)
    )
    fact = PhysicalTable(
        name="F_SALES",
        columns=(
            col("ID"),
            col("YYYYMMDD", "varchar(8)"),
            col("CUST_CD", "varchar"),
            col("AMT", "numeric"),
        ),
        primary_key=("ID",),
    )
    FK = ForeignKey
    schema = PhysicalSchema(
        tables=(dim, fact),
        foreign_keys=(FK("F_SALES", ("CUST_CD",), "D_CUST", ("CUST_CD",)),),
    )
    enriched, built = add_monthly_summaries(schema)
    assert len(built) == 1

    lay = LogicalLayer(
        models=(
            LogicalModel(
                name="V_F_SALES_MON",
                base_table="V_F_SALES_MON",
                fields=(
                    LogicalField(
                        name="YYYYMM",
                        type="varchar(6)",
                        source=FieldSource(
                            kind=FieldKind.BASE, table="V_F_SALES_MON", column="YYYYMM"
                        ),
                    ),
                    LogicalField(
                        name="AMT",
                        type="numeric",
                        source=FieldSource(
                            kind=FieldKind.AGGREGATED,
                            table="V_F_SALES_MON",
                            column="AMT",
                            aggregate="sum",
                        ),
                    ),
                ),
            ),
        ),
        source_table_count=2,
        source_column_count=5,
    )

    result = measure(lay, enriched, cursor=conn.cursor(), dialect="sqlite")

    assert result["measured"] is True
    assert result["retention"] > 0
    conn.close()


def test_fidelity_does_not_count_merged_aliases_as_losses():
    """동치 통합 후 보존율이 떨어지면 지표가 압축을 벌하는 셈이다."""

    from tablefold.relate.graph import SchemaGraph
    from tablefold.report import fidelity as fid_mod

    org = PhysicalTable(
        name="D_ORG",
        columns=(col("ORG_CD", "varchar"), col("ORG_ID"), col("NM", "varchar")),
        primary_key=("ORG_CD",),
    )
    schema = PhysicalSchema(tables=(org,), foreign_keys=())
    graph = SchemaGraph.build(schema)

    kept_field = LogicalField(
        name="ORG_CD",
        type="varchar",
        source=FieldSource(kind=FieldKind.BASE, table="D_ORG", column="ORG_CD"),
    )
    nm_field = LogicalField(
        name="NM",
        type="varchar",
        source=FieldSource(kind=FieldKind.BASE, table="D_ORG", column="NM"),
    )
    layer = LogicalLayer(
        models=(
            LogicalModel(name="M", base_table="D_ORG", fields=(kept_field, nm_field)),
        ),
        source_table_count=1,
        source_column_count=3,
    )

    aliases = frozenset({("d_org", "org_id")})
    measured = fid_mod.measure(layer, graph, merged_aliases=aliases)
    without = fid_mod.measure(layer, graph)

    assert measured.column_retention == 1.0  # ORG_ID 는 대표로 되찾을 수 있다
    assert without.column_retention < measured.column_retention
    table_row = measured.tables[0]
    assert "ORG_ID" in table_row.merged_equivalents
    assert "ORG_ID" not in table_row.dropped_values


def test_router_catalog_points_trend_questions_at_summaries():
    """요약 모델은 추세 질문의 1순위라는 방향을 카탈로그에서 미리 준다.

    놓치면 라우터는 상세 모델을 골라 정답을 내지만 원본 전체를 훑는다 — 틀린
    것보다 나쁘게 눈에 띄지 않는 낭비다.
    """
    from tablefold.t2sql.prompt import build_router_prompt

    summary = LogicalModel(
        name="V_F_SALES_MON",
        base_table="V_F_SALES_MON",
        fields=(
            LogicalField(
                name="YYYYMM",
                type="varchar(6)",
                source=FieldSource(
                    kind=FieldKind.BASE, table="V_F_SALES_MON", column="YYYYMM"
                ),
            ),
        ),
    )
    detail = LogicalModel(
        name="F_SALES",
        base_table="F_SALES",
        fields=(
            LogicalField(
                name="AMT",
                type="numeric",
                source=FieldSource(kind=FieldKind.BASE, table="F_SALES", column="AMT"),
            ),
        ),
    )
    layer = LogicalLayer(
        models=(summary, detail), source_table_count=2, source_column_count=2
    )

    catalog = str(build_router_prompt("월별 매출 추이 알려줘", layer))

    assert "월 입도 요약" in catalog
    # 상세 모델에는 붙지 않는다. 모든 모델에 붙으면 힌트가 아니라 소음이다.
    detail_block = catalog.split("### F_SALES", 1)[-1]
    assert "월 입도 요약" not in detail_block
