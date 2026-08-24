"""기간 파티션(월별 스냅샷) 결합.

원장을 월 단위로 스냅샷 떠 놓으면 폴드는 같은 질문을 스냅샷 수만큼의 모델로
갈라 놓았다. 여기서는 "합쳐야 하는 것"과 "절대 합치면 안 되는 것"을 고정한다.
"""

from __future__ import annotations

import sqlite3

import pytest

from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable
from tablefold.relate.consolidate import (
    consolidate_snapshots,
    partition_of,
)
from tablefold.relate.graph import SchemaGraph
from tablefold.rewrite.expand import expand
from tablefold.t2sql.preset import fold_star_schema


def col(name: str, type_: str = "bigint"):
    return PhysicalColumn(name=name, type=type_)


def snapshot_table(name: str):
    return PhysicalTable(
        name=name,
        columns=(col("ID"), col("CUST_CD", "varchar"), col("AMT", "numeric")),
        primary_key=("ID",),
    )


def ledger_schema(with_ledger: bool = False) -> PhysicalSchema:
    cust = PhysicalTable(
        name="D_CUST",
        columns=(col("CUST_CD", "varchar"), col("NM", "varchar")),
        primary_key=("CUST_CD",),
    )
    snaps = [snapshot_table(f"F_LEDGER_{ym}") for ym in ("202506", "202507", "202508")]
    tables = [cust]
    if with_ledger:
        tables.append(snapshot_table("F_LEDGER"))
    tables += snaps
    return PhysicalSchema(
        tables=tuple(tables),
        foreign_keys=tuple(
            ForeignKey(t.name, ("CUST_CD",), "D_CUST", ("CUST_CD",))
            for t in tables
            if t.name != "D_CUST"
        ),
    )


# ── 이름 해석 ────────────────────────────────────────────────────────────────


def test_period_suffixes_are_recognized():
    assert partition_of("F_LEDGER_202506") == ("F_LEDGER", "month", "202506")
    assert partition_of("SALES_20250817") == ("SALES", "day", "20250817")
    assert partition_of("PLAN_2025") == ("PLAN", "year", "2025")


def test_plain_numbers_are_not_periods():
    """``_12345`` 는 기간이 아니라 번호일 수 있다. 합치지 않는다."""
    assert partition_of("ADDR_12345") is None
    assert partition_of("F_LEDGER") is None


# ── 결합 ─────────────────────────────────────────────────────────────────────


def test_monthly_snapshots_become_one_virtual_fact():
    merged, reports = consolidate_snapshots(ledger_schema())

    names = {t.name for t in merged.tables}
    assert names == {"D_CUST", "F_LEDGER"}
    assert reports[0].members == (
        "F_LEDGER_202506",
        "F_LEDGER_202507",
        "F_LEDGER_202508",
    )
    assert reports[0].unified_edges == 1  # 셋 다 D_CUST 를 가리켰다


def test_the_discriminator_carries_the_snapshot_period():
    """어느 달에서 왔는지 몰라서는 "7월만"이 불가능해진다."""
    merged, _ = consolidate_snapshots(ledger_schema())

    virtual = merged.table("F_LEDGER")
    assert any(c.name == "SNAPSHOT_YYYYMM" for c in virtual.columns)
    for value in ("'202506'", "'202507'", "'202508'"):
        assert value in virtual.source_sql
    # 행은 쌓이는 것이 사실이다. 중복 제거는 답을 지어내는 일이다.
    assert "UNION ALL" in virtual.source_sql
    assert "\nUNION\n" not in virtual.source_sql


def test_a_name_collision_steps_aside_instead_of_overwriting():
    """원본 원장이 살아 있으면 그 이름을 덮지 않는다."""
    merged, reports = consolidate_snapshots(ledger_schema(with_ledger=True))

    names = {t.name for t in merged.tables}
    assert "F_LEDGER" in names  # 원본
    assert "F_LEDGER_ALL" in names  # 스냅샷 묶음
    assert reports[0].virtual.name == "F_LEDGER_ALL"


def test_edges_that_disagree_are_dropped_not_guessed():
    """엇갈린 엣지는 다수결도 하지 않고 전부 버린다.

    2 대 1로 새 차원을 따르자는 선택은 지어내기다. 관계가 없는 모델은
    "references no logical model"로 시끄럽게 죽지만, 잘못된 관계는 조용히
    이상한 행을 돌려준다.
    """
    old = snapshot_table("F_LEDGER_202412")
    new1 = snapshot_table("F_LEDGER_202506")
    new2 = snapshot_table("F_LEDGER_202507")
    old_dim = PhysicalTable(
        name="D_CUST",
        columns=(col("CUST_CD", "varchar"),),
        primary_key=("CUST_CD",),
    )
    new_dim = PhysicalTable(
        name="D_CUST_NEW",
        columns=(col("CUST_CD", "varchar"),),
        primary_key=("CUST_CD",),
    )
    schema = PhysicalSchema(
        tables=(old_dim, new_dim, old, new1, new2),
        foreign_keys=(
            ForeignKey("F_LEDGER_202412", ("CUST_CD",), "D_CUST", ("CUST_CD",)),
            ForeignKey("F_LEDGER_202506", ("CUST_CD",), "D_CUST_NEW", ("CUST_CD",)),
            ForeignKey("F_LEDGER_202507", ("CUST_CD",), "D_CUST_NEW", ("CUST_CD",)),
        ),
    )

    merged, reports = consolidate_snapshots(schema)

    assert reports[0].unified_edges == 0
    assert reports[0].dropped_outgoing_edges == 3
    assert not any(
        fk.from_table == "F_LEDGER" and fk.inferred for fk in merged.foreign_keys
    )


def test_different_structures_are_never_merged():
    """우연히 비슷한 표를 합치면 행을 지어내는 것이다."""
    a = snapshot_table("A_202506")
    b = PhysicalTable(
        name="A_202507",
        columns=(col("ID"), col("NOTE", "varchar")),  # 컬럼이 다르다
        primary_key=("ID",),
    )
    schema = PhysicalSchema(
        tables=(a, b),
        foreign_keys=(),
    )

    merged, reports = consolidate_snapshots(schema)

    assert reports == ()
    assert {t.name for t in merged.tables} == {"A_202506", "A_202507"}


def test_a_single_snapshot_is_not_a_partition():
    lone = snapshot_table("F_LEDGER_202506")
    schema = PhysicalSchema(tables=(lone,), foreign_keys=())

    merged, reports = consolidate_snapshots(schema)

    assert reports == ()
    assert merged.table("F_LEDGER_202506") is not None


def test_incoming_references_are_dropped_and_counted():
    """스냅샷을 참조하던 엣지는 옮기지 않고 센다. 조용히 넓히지 않는다."""
    snap = snapshot_table("F_LEDGER_202506")
    other = snapshot_table("F_LEDGER_202507")
    child = PhysicalTable(
        name="F_ADJ",
        columns=(col("ID"), col("LEDGER_ID")),
        primary_key=("ID",),
    )
    schema = PhysicalSchema(
        tables=(snap, other, child),
        foreign_keys=(ForeignKey("F_ADJ", ("LEDGER_ID",), "F_LEDGER_202506", ("ID",)),),
    )

    merged, reports = consolidate_snapshots(schema)

    assert reports[0].dropped_incoming_edges == 1
    assert not any(fk.to_table == "F_LEDGER" for fk in merged.foreign_keys)


# ── 폴드와 확장까지 (sqlite 실측) ────────────────────────────────────────────


@pytest.fixture
def ledger_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE D_CUST (CUST_CD TEXT PRIMARY KEY, NM TEXT);
        CREATE TABLE F_LEDGER_202506 (
            ID INTEGER PRIMARY KEY, CUST_CD TEXT, AMT NUMERIC);
        CREATE TABLE F_LEDGER_202507 (
            ID INTEGER PRIMARY KEY, CUST_CD TEXT, AMT NUMERIC);
        INSERT INTO D_CUST VALUES ('C1','철수'),('C2','영희');
        INSERT INTO F_LEDGER_202506 VALUES (1,'C1',100),(2,'C2',200);
        INSERT INTO F_LEDGER_202507 VALUES (1,'C1',300),(2,'C2',400);
        """
    )
    yield conn
    conn.close()


def test_the_folded_model_expands_and_executes_across_snapshots(ledger_db):
    """합쳐진 모델 하나의 질문이 두 달의 행을 모두 돌려준다.

    확장기가 가상 테이블을 서브쿼리로 펼치는 길을 타므로, 물리 계층(스냅샷
    원본 표들)은 손대지 않았는데도 답이 합쳐진다.
    """
    from tablefold.ir import ForeignKey as FK

    cust = PhysicalTable(
        name="D_CUST",
        columns=(col("CUST_CD", "varchar"), col("NM", "varchar")),
        primary_key=("CUST_CD",),
    )
    s1 = PhysicalTable(
        name="F_LEDGER_202506",
        columns=(col("ID"), col("CUST_CD", "varchar"), col("AMT", "numeric")),
        primary_key=("ID",),
    )
    s2 = PhysicalTable(
        name="F_LEDGER_202507",
        columns=(col("ID"), col("CUST_CD", "varchar"), col("AMT", "numeric")),
        primary_key=("ID",),
    )
    schema = PhysicalSchema(
        tables=(cust, s1, s2),
        foreign_keys=(
            FK("F_LEDGER_202506", ("CUST_CD",), "D_CUST", ("CUST_CD",)),
            FK("F_LEDGER_202507", ("CUST_CD",), "D_CUST", ("CUST_CD",)),
        ),
    )
    merged, _ = consolidate_snapshots(schema)

    result = fold_star_schema(merged)
    fact_models = [m.name for m in result.layer.models if m.name.startswith("F_")]
    assert len(fact_models) == 1  # 두 달이 한 모델로 접혔다

    physical = expand(
        "SELECT SUM(AMT) FROM F_LEDGER",
        result.layer,
        SchemaGraph.build(result.schema),
        dialect="tsql",
        pretty=False,
    ).sql

    cur = ledger_db.cursor()
    # sqlite 도 [식별자] 대괄호 인용을 받아들이므로 그대로 던진다.
    cur.execute(physical)
    total = cur.fetchone()[0]

    assert total == 100 + 200 + 300 + 400
