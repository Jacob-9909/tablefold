"""측정된 카디널리티가 준비 단계를 끝까지 통과하는지.

"측정하라, 주장하지 말라" — 커서가 있으면 distinct 를 세서 예산 경합에 넣고,
없으면 아무것도 지어내지 않는다. 이 파일은 그 약속이 prepare 단계에서도
지켜지는지 확인한다. 배분 규칙 자체의 검사는 :mod:`tests.test_compose` 에 있다.
"""

from __future__ import annotations

from tablefold.ir import ForeignKey, PhysicalSchema, PhysicalTable
from tablefold.t2sql.prepare import prepare_for_questions
from tests.conftest import column


class CardinalityCursor:
    """표별 ``COUNT(DISTINCT …)`` 일괄 질의에만 답하는 최소 커서.

    답은 표 순서대로 하나씩 꺼낸다. 질의 문자열을 파싱하지 않는다 — 검증 대상은
    SQL 문법이 아니라 "표당 한 번 물었는가"와 결과의 흐름이다.
    """

    def __init__(self, *rows: tuple) -> None:
        self.rows = list(rows)
        self.queries: list[str] = []

    def execute(self, operation: str, /) -> None:
        self.queries.append(operation)

    def fetchone(self) -> tuple | None:
        return self.rows.pop(0) if self.rows else None


def _schema() -> PhysicalSchema:
    org = PhysicalTable(
        name="D_ORG",
        columns=(column("ORG_CD", "varchar(7)"), column("HEAD_NM", "varchar(50)")),
        primary_key=("ORG_CD",),
    )
    sales = PhysicalTable(
        name="F_SALES",
        columns=(column("ORG_CD", "varchar(7)"), column("SALES_AMT", "float")),
    )
    return PhysicalSchema(
        tables=(org, sales),
        foreign_keys=(ForeignKey("F_SALES", ("ORG_CD",), "D_ORG", ("ORG_CD",)),),
    )


def test_measured_cardinality_is_counted_once_per_table_and_reported():
    # D_ORG(ORG_CD, HEAD_NM) 다음 F_SALES(ORG_CD, SALES_AMT) 순으로 답한다.
    cursor = CardinalityCursor((3, 40), (12, 5))

    prep = prepare_for_questions(
        _schema(),
        cursor=cursor,
        recover=False,
        consolidate_partitions=False,
        period_anchor=False,
        measure_cardinality=True,
    )

    # 네 컬럼을 잰 것이고, 표당 한 번씩만 물었다.
    assert prep.cardinality_measured == 4
    assert len(cursor.queries) == 2
    assert "카디널리티 4 측정" in prep.note


def test_without_a_cursor_measurement_is_not_claimed():
    """커서가 없으면 켜 두어도 거짓으로 채우지 않는다."""
    prep = prepare_for_questions(
        _schema(),
        measure_cardinality=True,
        recover=False,
        consolidate_partitions=False,
        period_anchor=False,
    )

    assert prep.cardinality_measured == 0
    assert "카디널리티" not in prep.note


def test_measured_cardinality_stays_silent_when_not_asked():
    cursor = CardinalityCursor()

    prep = prepare_for_questions(
        _schema(),
        cursor=cursor,
        recover=False,
        consolidate_partitions=False,
        period_anchor=False,
        measure_cardinality=False,
    )

    assert prep.cardinality_measured == 0
    assert cursor.queries == []
