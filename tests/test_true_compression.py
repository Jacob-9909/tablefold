"""정보량 측정 · 동치 컬럼 통합 · 월별 요약 — "진짜 압축" 삼인조.

공통 전제: 판정은 데이터로 한다. 이름이나 추측으로 줄인 것은 압축이 아니라
삭제다. sqlite 로 실측한다.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from tablefold.choose.cluster import SelectionPolicy
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
from tablefold.relate.consolidate import consolidate_snapshots
from tablefold.relate.equivalence import (
    dedupe_fields,
    find_equivalents,
)
from tablefold.relate.synthesize import add_monthly_summaries
from tablefold.report.information import measure
from tablefold.rewrite.expand import expand
from tablefold.t2sql.prepare import prepare_for_questions


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


# ── 스냅샷-vs-델타 판정 ───────────────────────────────────────────────────────


def _partition_schema(pk: str) -> PhysicalSchema:
    """월별 파티션 둘. pk 로 실체 키 구성을 바꿔가며 시험한다."""
    tables = [
        PhysicalTable(
            name=f"F_BAL_{ym}",
            columns=(
                col("YYYYMM", "varchar(6)"),
                col("ACCT_CD", "varchar"),
                col("BALANCE", "numeric"),
            ),
            primary_key=(pk, "ACCT_CD"),
        )
        for ym in ("202507", "202508")
    ]
    return PhysicalSchema(tables=tuple(tables), foreign_keys=())


def test_identical_entity_sets_across_partitions_flag_snapshot():
    """전체 계좌가 매달 재등장하면 잔고 스냅샷이다. SUM 하면 누적된다."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE F_BAL_202507 (
            YYYYMM TEXT, ACCT_CD TEXT, BALANCE NUMERIC,
            PRIMARY KEY (YYYYMM, ACCT_CD));
        CREATE TABLE F_BAL_202508 (
            YYYYMM TEXT, ACCT_CD TEXT, BALANCE NUMERIC,
            PRIMARY KEY (YYYYMM, ACCT_CD));
        """
    )
    rows = [(f"20250{i}", f"A{j:02d}", 100.0) for i in (7, 8) for j in range(1, 11)]
    conn.executemany("INSERT INTO F_BAL_202507 VALUES (?,?,?)", rows[:10])
    conn.executemany("INSERT INTO F_BAL_202508 VALUES (?,?,?)", rows[10:])
    schema = _partition_schema("YYYYMM")

    _, reports = consolidate_snapshots(schema, cursor=conn.cursor(), dialect="sqlite")

    assert reports[0].snapshot_like is True
    assert reports[0].entity_key_overlap == pytest.approx(1.0)
    conn.close()


def test_partial_entity_overlap_reads_as_delta():
    """매달 거래한 고객만 등장하는 델타는 겹침이 낮다. 요약이 만들어져야 한다."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE F_BAL_202507 (
            YYYYMM TEXT, ACCT_CD TEXT, BALANCE NUMERIC,
            PRIMARY KEY (YYYYMM, ACCT_CD));
        CREATE TABLE F_BAL_202508 (
            YYYYMM TEXT, ACCT_CD TEXT, BALANCE NUMERIC,
            PRIMARY KEY (YYYYMM, ACCT_CD));
        """
    )
    # 7월엔 10계좌, 8월엔 완전히 다른 10계좌 — 유사도 0.
    conn.executemany(
        "INSERT INTO F_BAL_202507 VALUES (?,?,?)",
        [("202507", f"A{j:02d}", 1.0) for j in range(1, 11)],
    )
    conn.executemany(
        "INSERT INTO F_BAL_202508 VALUES (?,?,?)",
        [("202508", f"B{j:02d}", 1.0) for j in range(1, 11)],
    )
    schema = _partition_schema("YYYYMM")

    merged, reports = consolidate_snapshots(
        schema, cursor=conn.cursor(), dialect="sqlite"
    )

    assert reports[0].snapshot_like is False
    assert reports[0].entity_key_overlap == 0.0
    # 델타니까 월 요약도 안전하게 만들 수 있다.
    _, built = add_monthly_summaries(merged, exclude=_excluded(reports))
    assert len(built) == 1
    conn.close()


def test_without_a_cursor_the_verdict_is_honestly_unknown():
    """값을 못 보면서 스냅샷이라 주장하면 측정의 취지가 사라진다."""
    schema = _partition_schema("YYYYMM")

    _, reports = consolidate_snapshots(schema)

    assert reports[0].snapshot_like is None
    assert reports[0].entity_key_overlap is None


def test_prepare_suppresses_summaries_for_snapshot_ledgers():
    """스냅샷형 원장은 요약을 만들지 않는다 — 틀린 답을 거절하는 것."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE F_BAL_202507 (
            YYYYMM TEXT, ACCT_CD TEXT, BALANCE NUMERIC,
            PRIMARY KEY (YYYYMM, ACCT_CD));
        CREATE TABLE F_BAL_202508 (
            YYYYMM TEXT, ACCT_CD TEXT, BALANCE NUMERIC,
            PRIMARY KEY (YYYYMM, ACCT_CD));
        INSERT INTO F_BAL_202507 VALUES ('202507','A01',100);
        INSERT INTO F_BAL_202508 VALUES ('202508','A01',200);
        """
    )
    schema = _partition_schema("YYYYMM")

    prep = prepare_for_questions(
        schema,
        cursor=conn.cursor(),
        consolidate_partitions=True,
        monthly_summaries=True,
    )

    summary_names = [
        m.name for m in prep.result.layer.models if m.name.startswith("V_")
    ]
    assert summary_names == []  # 스냅샷형은 SUM 요약 금지
    conn.close()


def _excluded(reports):
    return frozenset(r.virtual.name for r in reports if r.snapshot_like)


# ── 관계 축 (GROUPED) ─────────────────────────────────────────────────────────


def _link_schema() -> PhysicalSchema:
    orders = PhysicalTable(
        name="orders",
        columns=(
            col("order_id"),
            col("region_cd", "varchar"),
            col("amount", "numeric"),
        ),
        primary_key=("order_id",),
    )
    customers = PhysicalTable(
        name="customers",
        columns=(col("customer_id"), col("name", "varchar")),
        primary_key=("customer_id",),
    )
    link = PhysicalTable(
        name="order_customer_links",
        columns=(
            col("link_id"),
            col("order_id"),
            col("customer_id"),
        ),
        primary_key=("link_id",),
    )
    return PhysicalSchema(
        tables=(orders, customers, link),
        foreign_keys=(
            ForeignKey("order_customer_links", ("order_id",), "orders", ("order_id",)),
            ForeignKey(
                "order_customer_links", ("customer_id",), "customers", ("customer_id",)
            ),
        ),
    )


def test_groupable_children_are_only_made_when_asked():

    schema = _link_schema()

    from tablefold.choose.select import ExplicitSelector
    from tablefold.fold import fold

    anchors = ExplicitSelector(
        ("orders", "customers", "order_customer_links"),
        prune_redundant=False,
    )
    off = fold(
        schema,
        selector=anchors,
        policy=SelectionPolicy(max_areas=3),
        field_budget=10_000,
        include_aggregates=True,
        prefix_joined_fields=False,
        infer_missing_keys=False,
    ).layer
    on = fold(
        schema,
        selector=anchors,
        policy=SelectionPolicy(max_areas=3),
        field_budget=10_000,
        include_aggregates=True,
        prefix_joined_fields=False,
        expose_groupable_children=True,
        infer_missing_keys=False,
    ).layer

    assert not any(
        f.source.kind.value == "grouped" for m in off.models for f in m.fields
    )
    grouped = [
        f for m in on.models for f in m.fields if f.source.kind.value == "grouped"
    ]
    names = {f.name.split("_")[-1] for f in grouped}
    assert "name" in names  # 반대편(고객)의 라벨 컬럼이 축으로 나온다


def test_grouped_axis_answers_pair_questions_with_correct_counts():
    """관계 축으로 묶으면 조인이 다시 짜지고 개수는 DISTINCT 부모 기준이다.

    sqlite 로 실행해 값까지 확인한다 — 구조만 맞고 값이 틀리면 의미가 없다.
    """

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY, region_cd TEXT, amount NUMERIC);
        CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE order_customer_links (
            link_id INTEGER PRIMARY KEY, order_id INTEGER, customer_id INTEGER);
        INSERT INTO orders VALUES (1,'R1',100),(2,'R1',200),(3,'R2',300);
        INSERT INTO customers VALUES (10,'철수'),(20,'영희');
        -- 주문 1→철수, 주문 2→철수, 주문 3→영희
        INSERT INTO order_customer_links VALUES (1,1,10),(2,2,10),(3,3,20);
        """
    )
    from tablefold.choose.select import ExplicitSelector
    from tablefold.fold import fold

    schema = _link_schema()
    result = fold(
        schema,
        selector=ExplicitSelector(
            ("orders", "customers", "order_customer_links"), prune_redundant=False
        ),
        policy=SelectionPolicy(max_areas=3),
        field_budget=10_000,
        include_aggregates=True,
        prefix_joined_fields=False,
        expose_groupable_children=True,
        infer_missing_keys=False,
    )
    layer, graph = result.layer, result.graph

    model = next(m for m in layer.models if m.name == "orders")
    grouped = [f.name for f in model.fields if f.source.kind.value == "grouped"]
    name_field = next(n for n in grouped if n.endswith("_name"))
    count_field = next(n for n in grouped if n.endswith("_count"))

    expansion = expand(
        f"SELECT {name_field}, {count_field} FROM orders GROUP BY {name_field}",
        layer,
        graph,
        dialect="sqlite",
        pretty=False,
    )

    sql = expansion.sql.replace('"', "")
    rows = conn.execute(sql).fetchall()
    by_name = {r[0]: r[1] for r in rows}

    # 주문 2건이 철수로, 1건이 영희로 — DISTINCT 부모 기준이라 중복 없이 정확.
    assert by_name["철수"] == 2
    assert by_name["영희"] == 1
    conn.close()
