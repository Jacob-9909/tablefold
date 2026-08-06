"""키는 있는데 그 키를 소유한 테이블이 없을 때, 테이블을 세운다.

폴드는 앵커에서 FK 엣지를 따라 접는다. 엣지가 없으면 접을 수 없고, 엣지는
*테이블* 사이에만 놓인다. 그래서 여러 팩트가 같은 키를 들고 있어도 그 키를
소유한 차원 테이블이 없으면 서로 이어지지 않는다.

실측 스키마(NL2SQL 19테이블)가 정확히 그렇다 — ``YYYYMM`` 이 7개 팩트,
``YYYYMMDD`` 가 3개 팩트에 있는데 캘린더 차원이 없다. 결과로 기간은
``filter_only`` 통로로만 다뤄지고, "월별 매출 대 계획"처럼 **기간 입도에서
묻는 질문을 받을 앵커가 존재하지 않는다.**

여기서 하는 일은 그 키를 모아 가상 테이블을 하나 세우는 것뿐이다. 물리 테이블이
생기지 않으므로 데이터베이스는 건드리지 않는다. 확장이 이 테이블을 만나면
:attr:`PhysicalTable.source_sql` 을 서브쿼리로 펼친다.

**왜 LLM을 쓰지 않는가.** 이건 셀 수 있는 것이다. 후보는 이름과 타입으로 좁히고
판정은 데이터로 한다 — 이름만 보고 고르면 ``BS_ACCT_CD varchar(8)`` 같은 코드
컬럼이 날짜로 잡힌다. 그래서 아래 판정은 길이가 아니라 **이름 어휘**에 건다.
"""

from __future__ import annotations

import re
from dataclasses import replace

from tablefold.ir import (
    ForeignKey,
    PhysicalColumn,
    PhysicalSchema,
    PhysicalTable,
)

PERIOD_ANCHOR = "V_PERIOD"
"""합성된 기간 차원의 이름. 물리 테이블과 부딪히지 않게 접두사를 둔다."""

MONTH_KEY = "YYYYMM"

_MONTH_NAMES = frozenset({"yyyymm", "yyyy_mm", "base_yyyymm", "std_yyyymm"})
_DAY_NAMES = frozenset({"yyyymmdd", "yyyy_mm_dd", "base_yyyymmdd", "std_yyyymmdd"})

_STRING_TYPE = re.compile(r"\b(var)?char|text|nvarchar", re.IGNORECASE)


def _grain(column: PhysicalColumn) -> str | None:
    """``"month"`` / ``"day"`` / 기간이 아니면 ``None``.

    길이로 판정하지 않는다. 실측 스키마의 ``BS_ACCT_CD`` 도 ``varchar(8)`` 이고,
    길이만 보면 계정코드가 날짜가 된다. 이름 어휘가 좁고 확실한 신호다.
    """
    if not _STRING_TYPE.search(column.type):
        return None
    lowered = column.name.lower()
    if lowered in _DAY_NAMES:
        return "day"
    if lowered in _MONTH_NAMES:
        return "month"
    return None


def add_period_anchor(
    schema: PhysicalSchema, *, name: str = PERIOD_ANCHOR
) -> PhysicalSchema:
    """기간 키를 들고 있는 테이블들을 이어 줄 가상 차원을 붙인다.

    아무것도 할 게 없으면 *schema* 를 **그대로** 돌려준다. 호출부가 동일성으로
    "합성이 일어났는가"를 볼 수 있게 하려는 것이다.

    붙이지 않는 경우가 셋이다: 이미 그 키를 소유한 테이블이 있을 때(진짜 캘린더가
    있다), 기간 컬럼을 가진 테이블이 하나뿐일 때(이어 줄 것이 없다), 그리고 이름이
    이미 쓰이고 있을 때.
    """
    if schema.table(name) is not None:
        return schema

    bearers: list[tuple[PhysicalTable, PhysicalColumn, str]] = []
    for table in schema.tables:
        for column in table.columns:
            grain = _grain(column)
            if grain is None:
                continue
            # 이미 이 키를 소유한 테이블이 있으면 그것이 캘린더다. 손대지 않는다.
            if table.primary_key == (column.name,):
                return schema
            bearers.append((table, column, grain))

    if len({t.name for t, _, _ in bearers}) < 2:
        return schema

    anchor = PhysicalTable(
        name=name,
        columns=(
            PhysicalColumn(name=MONTH_KEY, type="varchar(6)", comment="기준년월"),
            PhysicalColumn(name="YYYY", type="varchar(4)", comment="기준연도"),
        ),
        primary_key=(MONTH_KEY,),
        comment="기간(가상) — 팩트들의 기간 키를 모아 세운 차원",
        source_sql=_anchor_sql(bearers),
    )
    return replace(
        schema,
        tables=(*schema.tables, anchor),
        foreign_keys=(*schema.foreign_keys, *_edges(bearers, name)),
    )


def _month_expression(column: PhysicalColumn, grain: str) -> str:
    """자식 컬럼에서 월 키를 만드는 식. 월 입도면 컬럼 그대로다.

    방언 중립으로 적는다 — ``sqlglot`` 이 파싱한 뒤 대상 방언으로 다시 쓴다.
    """
    if grain == "month":
        return column.name
    return f"SUBSTRING({column.name}, 1, 6)"


def _anchor_sql(
    bearers: list[tuple[PhysicalTable, PhysicalColumn, str]],
) -> str:
    """기간 키의 합집합.

    ``UNION ALL`` 이 아니라 ``UNION`` 이다. 앵커는 키당 **정확히 한 행**이어야
    하고, 그렇지 않으면 앵커 자신이 복제되어 자식 집계가 통째로 부풀어 오른다 —
    막으려던 바로 그 오류가 앵커에서 일어난다.
    """
    parts = [
        f"SELECT {_month_expression(column, grain)} AS {MONTH_KEY} "
        f"FROM {table.qualified_name}"
        for table, column, grain in bearers
    ]
    union = " UNION ".join(parts)
    return (
        f"SELECT {MONTH_KEY}, SUBSTRING({MONTH_KEY}, 1, 4) AS YYYY "
        f"FROM ({union}) AS periods"
    )


def _edges(
    bearers: list[tuple[PhysicalTable, PhysicalColumn, str]], anchor: str
) -> tuple[ForeignKey, ...]:
    return tuple(
        ForeignKey(
            from_table=table.name,
            from_columns=(column.name,),
            to_table=anchor,
            to_columns=(MONTH_KEY,),
            inferred=True,
            # 월 입도는 값이 그대로 같다. 일 입도만 식이 필요하다.
            key_expressions=(
                None if grain == "month" else (_month_expression(column, grain),)
            ),
        )
        for table, column, grain in bearers
    )
