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


# ── 월별 요약 (큐브 라이트) ───────────────────────────────────────────────────
#
# 와이드 모델은 조인을 없애지만 행을 줄이지는 않는다. 원장 1000만 행의 월별
# 추세를 묻는 질문도 결국 1000만 행을 훑는다. 팩트마다 **월 입도 요약 가상
# 테이블**을 하나 세워 두면, 추세 질문은 요약을 읽고 상세 질문만 원본을 읽는
# 식으로 갈린다. 요약은 GROUP BY 로 만드므로 행 압축이 실제로 일어난다 — 이
# 저장소에서 몇 안 되는 진짜 정보 축소다.

SUMMARY_PREFIX = "V_"
SUMMARY_SUFFIX = "_MON"


def add_monthly_summaries(
    schema: PhysicalSchema,
    *,
    facts: tuple[str, ...] | None = None,
    max_measures: int = 8,
    exclude: frozenset[str] = frozenset(),
) -> tuple[PhysicalSchema, tuple[str, ...]]:
    """팩트 표마다 월 입도 요약 가상 테이블을 붙인 스키마.

    ``exclude`` 는 요약을 만들지 않을 표 이름이다. 스냅샷형(월말 잔고) 원장이
    여기 들어온다 — 그 행들을 월로 묶어 ``SUM`` 하면 같은 계좌의 잔고가 기간
    수만큼 누적된다. 묶는 대신 건너뛴다는 것은 "답을 포기"가 아니라
    "틀린 답을 거절"이다.

    ``facts`` 를 비우면 나가는 키가 있고 참조당하지 않는 표(= 전형적 팩트)를
    고른다. 가상 표도 후보다 — 파티션 결합으로 합쳐진 원장의 월 추세를 묻는
    질문이 여전히 전 행을 훑는다면 압축이 반쪽이다. 요약의 골격은:

    * 기간 키 — ``YYYYMMDD`` 는 앞 여섯 자리로 접고(파생키 규칙과 같은 식),
      ``YYYYMM`` 은 그대로 둔다.
    * 차원 키 — 이 팩트가 참조하는 외래 키 컬럼. 요약도 같은 차원에 붙어야
      "조직별 월 매출"이 요약 모델에서 바로 답힌다.
    * 실측값 — 숫자 컬럼의 SUM 과 줄 수 COUNT. 상한(``max_measures``)은 요약
      모델이 또 넓어지는 것을 막는 자리다.

    기간 키가 아예 없는 팩트는 건너뛴다 — 무엇으로 묶을지 모르는 요약은
    지어내는 것이다.
    """
    from tablefold.relate.graph import SchemaGraph

    graph = SchemaGraph.build(schema)
    if facts is None:
        # 후보는 "기간 키를 가진 표"다. 차수로 고르면 파티션 결합처럼 간선이
        # 없는 가상 원장이 빠진다 — 그런 원장의 월 추세야말로 요약할 가치가
        # 크다. 기간 앵커 자신과 이미 만든 요약은 다시 요약하지 않는다.
        facts = tuple(
            t.name
            for t in schema.tables
            if t.name != PERIOD_ANCHOR
            and not t.name.startswith(SUMMARY_PREFIX)
            and _month_key_of(t)[0] is not None
            and any(c.is_numeric for c in t.columns)
        )

    taken = {t.name.lower() for t in schema.tables}
    added_tables: list[PhysicalTable] = []
    added_edges: list[ForeignKey] = []
    built: list[str] = []

    for fact_name in facts:
        if fact_name in exclude:
            continue
        fact = schema.table(fact_name)
        if fact is None:
            continue

        month_column, grain = _month_key_of(fact)
        if month_column is None:
            continue

        fk_columns = [
            c
            for fk in graph.outgoing(fact.name)
            for c in fk.from_columns
            if c.lower() != month_column.name.lower()
        ]
        measures = [c.name for c in fact.columns if c.is_numeric][:max_measures]
        # SELECT 의 식과 GROUP BY 의 식이 같아야 한다. SELECT 는 여섯 자리로
        # 접고 GROUP BY 는 날짜 전체를 쓰면 요약이 아니라 일별 복제가 된다.
        month_select, month_group = _month_expr(month_column.name, grain)

        select_parts = [month_select, *fk_columns]
        agg_parts = [f"SUM({m}) AS {m}" for m in measures]
        agg_parts.append("COUNT(*) AS ROW_CNT")
        select_parts += agg_parts

        source_sql = (
            f"SELECT {', '.join(select_parts)} FROM {fact.qualified_name} "
            f"GROUP BY {', '.join([month_group, *fk_columns])}"
        )

        summary_name = f"{SUMMARY_PREFIX}{fact.name}{SUMMARY_SUFFIX}"
        stem = 1
        while summary_name.lower() in taken:
            summary_name = f"{summary_name}_{stem}"
            stem += 1
        taken.add(summary_name.lower())

        columns = [
            PhysicalColumn(
                name=MONTH_KEY,
                type="varchar(6)",
                comment="기준년월",
            ),
            *[replace(c, comment=None) for c in fact.columns if c.name in fk_columns],
            *[
                replace(c, comment=f"요약 합계 ({fact.name}.{c.name})")
                for c in fact.columns
                if c.name in measures
            ],
            PhysicalColumn(name="ROW_CNT", type="bigint", comment="원본 줄 수"),
        ]
        added_tables.append(
            PhysicalTable(
                name=summary_name,
                columns=tuple(columns),
                primary_key=(),
                row_estimate=None,
                comment=f"{fact.name} 의 월 입도 요약 — 추세 질문은 이쪽을 읽는다",
                source_sql=source_sql,
            )
        )

        # 요약이 참조하는 차원은 원 팩트와 같다. GROUP BY 에 그 키를 넣었으므로
        # 관계가 그대로 성립한다.
        for fk in graph.outgoing(fact.name):
            kept = tuple(
                c for c in fk.from_columns if c.lower() != month_column.name.lower()
            )
            if not kept:
                continue
            added_edges.append(
                ForeignKey(
                    from_table=summary_name,
                    from_columns=kept,
                    to_table=fk.to_table,
                    to_columns=fk.to_columns,
                    inferred=True,
                    confidence=fk.confidence,
                    key_expressions=fk.key_expressions,
                    condition=fk.condition,
                )
            )
        built.append(summary_name)

    if not added_tables:
        return schema, ()

    return replace(
        schema,
        tables=(*schema.tables, *added_tables),
        foreign_keys=(*schema.foreign_keys, *added_edges),
    ), tuple(built)


def _month_key_of(fact: PhysicalTable):
    """팩트가 들고 있는 기간 키를 고른다. 월 키가 최선, 날짜 키는 접어 쓴다.

    날짜 키를 거절할 이유가 없다 — 요약은 어차피 앞 여섯 자리로 묶는다. 캘린더
    차원이 있고 없고도 무관하다: 요약의 GROUP BY 는 물리 표 안에서 끝난다.
    """
    best = None
    for column in fact.columns:
        grain = _grain(column)
        if grain == "month":
            return column, "month"
        if grain == "day" and best is None:
            best = (column, "day")
    return best or (None, None)


def _month_expr(column: str, grain: str) -> tuple[str, str]:
    """`(SELECT 절 식, GROUP BY 절 식)`. 두 식은 반드시 같은 값을 묶어야 한다."""
    if grain == "month":
        return f"{column} AS {MONTH_KEY}", column
    # 방언 중립. 확장기가 sqlglot 으로 대상 방언으로 다시 쓴다.
    folded = f"SUBSTRING({column}, 1, 6)"
    return f"{folded} AS {MONTH_KEY}", folded
