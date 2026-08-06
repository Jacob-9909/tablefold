"""데이터로 검증한 관계 복구.

스키마만 보는 복구는 "가능한" 관계를 전부 만든다. 여기 있는 것들은 가능한 것과
실재하는 것을 가르는 규칙이고, 그 판단이 틀리면 그래프가 조각나거나 없는 관계가
모델에 들어간다 — 둘 다 조용히 답을 바꾼다.
"""

from __future__ import annotations

from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable
from tablefold.relate.validate import (
    quoted,
    recover_with_data,
    unique_single_keys,
    validate_foreign_keys,
    violation_rate,
)


class FakeCursor:
    """``execute`` / ``fetchone`` 만 흉내 낸다. 질의 문자열도 기록해 둔다."""

    def __init__(self, answers: list[tuple]) -> None:
        self.answers = list(answers)
        self.queries: list[str] = []

    def execute(self, operation: str, /) -> None:
        self.queries.append(operation)

    def fetchone(self) -> tuple | None:
        return self.answers.pop(0) if self.answers else None


def fk(from_table: str, column: str, to_table: str) -> ForeignKey:
    return ForeignKey(from_table, (column,), to_table, (column,), inferred=True)


# ── 인용 ──────────────────────────────────────────────────────────────────────


def test_identifiers_are_quoted_for_the_dialect():
    """방언별 인용 규칙을 직접 쓰지 않는다. 예약어와 대소문자가 걸린다."""
    assert quoted("ORDER", "tsql") == "[ORDER]"
    assert quoted("ORDER", "postgres") == '"ORDER"'


# ── 위반율 ────────────────────────────────────────────────────────────────────


def test_a_perfect_match_has_no_violations():
    cursor = FakeCursor([(0,), (1000,)])

    assert violation_rate(fk("F_SALES", "ORG_CD", "D_ORG"), cursor) == 0.0


def test_orphan_rows_raise_the_violation_rate():
    cursor = FakeCursor([(50,), (1000,)])

    assert violation_rate(fk("F_SALES", "ORG_CD", "D_ORG"), cursor) == 0.05


def test_nulls_are_not_counted_as_violations():
    """값이 없는 것과 잘못된 곳을 가리키는 것은 다르다."""
    cursor = FakeCursor([(0,), (1000,)])
    violation_rate(fk("F_SALES", "ORG_CD", "D_ORG"), cursor)

    assert all("IS NOT NULL" in q for q in cursor.queries)


def test_an_empty_source_table_is_treated_as_a_violation():
    """근거가 없으면 통과시키지 않는다 — 없는 관계가 모델에 들어가는 편이 나쁘다."""
    cursor = FakeCursor([(0,), (0,)])

    assert violation_rate(fk("F_SALES", "ORG_CD", "D_ORG"), cursor) == 1.0


# ── 후보 거르기 ───────────────────────────────────────────────────────────────


def test_a_candidate_over_the_tolerance_is_dropped():
    candidates = (fk("F_SALES", "ORG_CD", "D_ORG"),)
    cursor = FakeCursor([(500,), (1000,)])  # 50% 위반

    assert validate_foreign_keys(candidates, cursor) == ()


def test_a_surviving_candidate_carries_the_measured_confidence():
    """``infer_from_primary_keys`` 의 0.9 는 자리표시자다. 잰 값으로 덮어쓴다."""
    candidates = (fk("F_SALES", "ORG_CD", "D_ORG"),)
    cursor = FakeCursor([(5,), (1000,)])  # 0.5% — 허용 안쪽

    (kept,) = validate_foreign_keys(candidates, cursor)

    assert kept.confidence == 0.995


def test_every_target_the_data_allows_is_kept():
    """같은 컬럼이 여러 차원에 들어맞으면 전부 남긴다.

    한때 가장 좁은 차원 하나만 골랐고, 그게 그래프를 조각냈다 — 팩트들이 서로 다른
    조직 차원으로 끌려가 한 모델에서 만나지 못했다.
    """
    candidates = (
        fk("F_SALES", "ORG_CD", "D_ORG"),
        fk("F_SALES", "ORG_CD", "D_SA_ORG"),
        fk("F_SALES", "ORG_CD", "D_FI_ORG"),
    )
    cursor = FakeCursor([(0,), (100,)] * 3)

    kept = validate_foreign_keys(candidates, cursor)

    assert {k.to_table for k in kept} == {"D_ORG", "D_SA_ORG", "D_FI_ORG"}


def test_the_narrower_key_wins_between_the_same_two_tables():
    """넓은 키는 좁은 키를 포함하므로 조인 조건이 중복된다."""
    wide = ForeignKey(
        "F_SALES", ("GROUP_CD", "ITEM_CD"), "D_ITEM", ("GROUP_CD", "ITEM_CD")
    )
    narrow = fk("F_SALES", "ITEM_CD", "D_ITEM")
    cursor = FakeCursor([(0,), (100,)] * 2)

    kept = validate_foreign_keys((wide, narrow), cursor)

    assert len(kept) == 1
    assert kept[0].from_columns == ("ITEM_CD",)


# ── 부분 유일 키 ──────────────────────────────────────────────────────────────


def test_a_unique_subset_of_a_composite_key_is_found():
    """웨어하우스 차원은 기본 키에 계층 단계를 함께 넣는다.

    팩트는 말단 코드만 들고 있어서 전체 키가 안 맞는다. 그 부분 키로도 행이
    유일하면 참조 대상이 될 수 있고, 유일성은 데이터로만 확인된다.
    """
    item = PhysicalTable(
        name="D_ITEM",
        columns=(
            PhysicalColumn("ITEM_GROUP_CD", "varchar"),
            PhysicalColumn("ITEM_CD", "varchar"),
        ),
        primary_key=("ITEM_GROUP_CD", "ITEM_CD"),
    )
    schema = PhysicalSchema(tables=(item,))
    # ITEM_GROUP_CD 는 중복(100행 중 5종), ITEM_CD 는 유일.
    cursor = FakeCursor([(100, 5), (100, 100)])

    assert unique_single_keys(schema, cursor) == {"D_ITEM": ("ITEM_CD",)}


def test_a_single_column_key_needs_no_probe():
    """이미 유일하다고 선언된 키를 데이터로 다시 확인하지 않는다."""
    org = PhysicalTable(
        name="D_ORG",
        columns=(PhysicalColumn("ORG_CD", "varchar"),),
        primary_key=("ORG_CD",),
    )
    cursor = FakeCursor([])

    assert unique_single_keys(PhysicalSchema(tables=(org,)), cursor) == {}
    assert cursor.queries == []


# ── 전체 경로 ─────────────────────────────────────────────────────────────────


def test_recovery_targets_only_tables_nothing_references_out_of():
    """팩트끼리 공유 키로 엮이면 실재하지 않는 관계가 대량으로 생긴다."""
    org = PhysicalTable(
        name="D_ORG",
        columns=(
            PhysicalColumn("ORG_CD", "varchar"),
            PhysicalColumn("HEAD_NM", "varchar"),
        ),
        primary_key=("ORG_CD",),
    )
    sales = PhysicalTable(
        name="F_SALES",
        columns=(
            PhysicalColumn("YYYYMMDD", "varchar"),
            PhysicalColumn("ORG_CD", "varchar"),
            PhysicalColumn("SALES_AMT", "numeric"),
        ),
        primary_key=("YYYYMMDD", "ORG_CD"),
    )
    stock = PhysicalTable(
        name="F_STOCK",
        columns=(
            PhysicalColumn("YYYYMM", "varchar"),
            PhysicalColumn("ORG_CD", "varchar"),
            PhysicalColumn("STOCK_AMT", "numeric"),
        ),
        primary_key=("YYYYMM", "ORG_CD"),
    )
    schema = PhysicalSchema(tables=(org, sales, stock))
    # 복합 키 컬럼마다 유일성 탐색(팩트 둘 × 2컬럼) + 후보 2건의 위반율 2회씩.
    cursor = FakeCursor(
        [(10, 3), (10, 3), (10, 3), (10, 3), (0,), (100,), (0,), (100,)]
    )

    recovered_schema, recovered = recover_with_data(schema, cursor)

    assert {(f.from_table, f.to_table) for f in recovered} == {
        ("F_SALES", "D_ORG"),
        ("F_STOCK", "D_ORG"),
    }
    assert len(recovered_schema.foreign_keys) == 2


def test_recovery_returns_the_schema_untouched_when_nothing_survives():
    org = PhysicalTable(
        name="D_ORG",
        columns=(PhysicalColumn("ORG_CD", "varchar"),),
        primary_key=("ORG_CD",),
    )
    sales = PhysicalTable(
        name="F_SALES",
        columns=(
            PhysicalColumn("YYYYMMDD", "varchar"),
            PhysicalColumn("ORG_CD", "varchar"),
        ),
        primary_key=("YYYYMMDD", "ORG_CD"),
    )
    schema = PhysicalSchema(tables=(org, sales))
    cursor = FakeCursor([(10, 3), (10, 3), (99,), (100,)])  # 99% 위반

    recovered_schema, recovered = recover_with_data(schema, cursor)

    assert recovered == ()
    assert recovered_schema is schema
