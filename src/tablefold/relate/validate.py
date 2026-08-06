"""복구한 관계를 **실제 데이터로** 검증한다.

:func:`~tablefold.relate.keys.infer_from_primary_keys` 는 스키마만 보고 "가능한"
관계를 만든다. 가능한 것과 실재하는 것은 다르다 — ``ORG_CD`` 를 가진 표는
``D_ORG`` · ``D_SA_ORG`` · ``D_FI_ORG`` 어느 쪽이든 가리킬 수 있고, 스키마는 어느
쪽인지 말해 주지 않는다. 여기서는 세어 본다: 참조 대상에 없는 값이 몇 %인가.

임계값을 넘는 후보는 버리고, 남은 것의 ``confidence`` 는 실측 위반율로 덮어쓴다.
``infer_from_primary_keys`` 가 붙이는 0.9 는 "스키마만 보고 지은 후보"라는 뜻의
자리표시자이므로, 관측한 값으로 바꿔야 뒤에서 읽는 쪽이 속지 않는다.

**데이터가 통과시킨 관계는 전부 남긴다.** 한때 같은 컬럼에서 나온 후보 중 가장
좁은 차원 하나만 골랐는데, 그게 그래프를 조각냈다 — ``F_SALES`` 는 ``D_SA_ORG``
로, ``F_STOCK`` 은 ``D_FI_ORG`` 로 끌려가 한 모델에서 만나지 못했다. 실측에서
업무 주제 9개 중 구매와 인사가 그래서 답이 안 됐다. 전부 남기면 앵커 선택이
어느 쪽으로든 갈 수 있고, 넓은 차원이 늘어도 중복 앵커 제거가 뒤에서 걸러 준다.

이 모듈은 커서를 받되 어느 드라이버인지는 모른다. ``execute`` / ``fetchone`` 만
쓰므로 pymssql · psycopg · sqlite3 어디서나 돈다.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from sqlglot import exp

from tablefold.ir import ForeignKey, PhysicalSchema

DEFAULT_VIOLATION_TOLERANCE = 0.01
"""참조 대상에 없는 값의 허용 비율.

0 이 아닌 이유는 웨어하우스에 결측 코드가 늘 조금씩 있기 때문이다. 그걸로 실재하는
관계를 버리면 그래프가 조각난다. 1% 는 실측에서 정한 값이다.
"""


class Cursor(Protocol):
    """``execute`` / ``fetchone`` 만 쓴다. 드라이버는 상관없다."""

    def execute(self, operation: str, /) -> object: ...

    def fetchone(self) -> tuple | None: ...


def quoted(name: str, dialect: str) -> str:
    """식별자를 방언에 맞게 인용한다.

    카탈로그에서 읽은 이름이지 사용자 입력이 아니지만, 인용은 해야 한다 — 예약어와
    대소문자 때문이다. 방언별 규칙(MSSQL 은 ``[name]``, PostgreSQL 은 ``"name"``)을
    직접 쓰지 않고 sqlglot 에 맡긴다.
    """
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def unique_single_keys(
    schema: PhysicalSchema, cursor: Cursor, *, dialect: str = "tsql"
) -> dict[str, tuple[str, ...]]:
    """복합 기본 키 중 **한 컬럼만으로도 행이 유일한** 것을 찾는다.

    웨어하우스 차원은 기본 키에 계층 단계를 함께 넣는 일이 흔한데(``D_ITEM`` 의
    ``(ITEM_GROUP_CD, ITEM_CD)``), 팩트는 말단 코드만 들고 있어서 전체 키가 맞지
    않는다. 그 부분 키로도 유일하면 참조 대상이 될 수 있다 — 유일성은 데이터로만
    확인되므로 여기서 센다.
    """
    found: dict[str, tuple[str, ...]] = {}
    for table in schema.tables:
        if len(table.primary_key) < 2:
            continue
        for column in table.primary_key:
            if _is_load_metadata(column):
                continue
            cursor.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT {quoted(column, dialect)}) "
                f"FROM {_table_ref(table.schema, table.name, dialect)}"
            )
            row = cursor.fetchone()
            if row and len(row) > 1 and row[0] and row[0] == row[1]:
                found[table.name] = (column,)
                break
    return found


def violation_rate(fk: ForeignKey, cursor: Cursor, *, dialect: str = "tsql") -> float:
    """``fk`` 가 가리키는 곳에 없는 값의 비율. 0.0 이면 완전히 들어맞는다.

    ``NULL`` 은 세지 않는다 — 값이 없는 것과 잘못된 곳을 가리키는 것은 다르다.
    참조하는 쪽에 행이 하나도 없으면 1.0 을 돌려준다. 근거 없는 관계를 통과시키는
    것보다 버리는 편이 낫다.
    """
    source = "s"
    target = "t"
    on = " AND ".join(
        f"{target}.{quoted(b, dialect)} = {source}.{quoted(a, dialect)}"
        for a, b in zip(fk.from_columns, fk.to_columns, strict=True)
    )
    not_null = " AND ".join(
        f"{source}.{quoted(c, dialect)} IS NOT NULL" for c in fk.from_columns
    )
    from_ref = _table_ref(None, fk.from_table, dialect)
    to_ref = _table_ref(None, fk.to_table, dialect)

    cursor.execute(
        f"SELECT COUNT(*) FROM {from_ref} {source} WHERE {not_null} "
        f"AND NOT EXISTS (SELECT 1 FROM {to_ref} {target} WHERE {on})"
    )
    orphans = _first(cursor.fetchone())
    cursor.execute(f"SELECT COUNT(*) FROM {from_ref} {source} WHERE {not_null}")
    total = _first(cursor.fetchone())
    return orphans / total if total else 1.0


def validate_foreign_keys(
    candidates: tuple[ForeignKey, ...],
    cursor: Cursor,
    *,
    tolerance: float = DEFAULT_VIOLATION_TOLERANCE,
    dialect: str = "tsql",
) -> tuple[ForeignKey, ...]:
    """데이터가 통과시킨 후보만, 실측 확신도를 붙여 돌려준다.

    같은 두 표 사이에 후보가 여럿이면 **좁은 키** 쪽만 남긴다. 넓은 키는 좁은 키를
    포함하므로 조인 조건이 중복된다.
    """
    kept: list[ForeignKey] = []
    for candidate in candidates:
        rate = violation_rate(candidate, cursor, dialect=dialect)
        if rate > tolerance:
            continue
        kept.append(replace(candidate, confidence=round(1.0 - rate, 4)))

    narrowest: dict[tuple[str, str], ForeignKey] = {}
    for fk in kept:
        pair = (fk.from_table.lower(), fk.to_table.lower())
        if pair not in narrowest or len(fk.from_columns) < len(
            narrowest[pair].from_columns
        ):
            narrowest[pair] = fk
    return tuple(narrowest.values())


def recover_with_data(
    schema: PhysicalSchema,
    cursor: Cursor,
    *,
    targets: tuple[str, ...] | None = None,
    tolerance: float = DEFAULT_VIOLATION_TOLERANCE,
    dialect: str = "tsql",
) -> tuple[PhysicalSchema, tuple[ForeignKey, ...]]:
    """기본 키로 관계를 짓고, 데이터로 걸러 붙인 스키마와 그 관계들.

    ``targets`` 는 참조 *대상* 이 될 수 있는 표를 제한한다. 비워 두면 나가는 키가
    없는 표(= 차원)를 쓴다 — 팩트끼리 공유 키로 엮이는 것을 막는다.
    """
    from tablefold.relate.graph import SchemaGraph
    from tablefold.relate.keys import infer_from_primary_keys

    if targets is None:
        graph = SchemaGraph.build(schema)
        targets = tuple(
            t.name
            for t in schema.tables
            if not graph.out_degree(t.name) and t.primary_key
        )
    if not targets:
        return schema, ()

    candidates = infer_from_primary_keys(
        schema,
        targets=targets,
        unique_subsets=unique_single_keys(schema, cursor, dialect=dialect),
    )
    recovered = validate_foreign_keys(
        candidates, cursor, tolerance=tolerance, dialect=dialect
    )
    if not recovered:
        return schema, ()
    return schema.with_foreign_keys(schema.foreign_keys + recovered), recovered


# ── 잡일 ──────────────────────────────────────────────────────────────────────

_LOAD_METADATA = frozenset({"load_dt", "load_user", "etl_dt", "etl_id"})


def _is_load_metadata(column: str) -> bool:
    return column.lower() in _LOAD_METADATA


def _table_ref(schema: str | None, name: str, dialect: str) -> str:
    if schema:
        return f"{quoted(schema, dialect)}.{quoted(name, dialect)}"
    return quoted(name, dialect)


def _first(row: tuple | None) -> int:
    return int(row[0]) if row and row[0] is not None else 0
