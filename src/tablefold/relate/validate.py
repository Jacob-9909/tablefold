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

try:  # 값 포함 탐사의 예산 기본값. 순환 참조를 피하려 늦게 읽는다.
    from tablefold.relate.discover import DEFAULT_MAX_PROBES
except ImportError:  # pragma: no cover — discover 가 항상 함께 배포된다
    DEFAULT_MAX_PROBES = 24


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


# 복합 기본 키 유일성 검사를 건너뛸 행 수. ``COUNT(DISTINCT …)`` 는 인덱스가
# 있어도 큰 표에서 풀스캔이다 — 폴드 한 번에 수십 개 표를 검사하니 상한 없이는
# 웨어하우스에서 질의 시간이 지배된다. 추정치가 이 값을 넘으면 그 표는 건너뛰고
# 부분 키 복구 대상에서 빠진다 (덜 복구하는 것이 몇 분 걸리는 폴드보다 낫다).
DEFAULT_MAX_SCAN_ROWS = 50_000_000


def unique_single_keys(
    schema: PhysicalSchema,
    cursor: Cursor,
    *,
    dialect: str = "tsql",
    max_rows: int | None = DEFAULT_MAX_SCAN_ROWS,
) -> dict[str, tuple[str, ...]]:
    """복합 기본 키 중 **한 컬럼만으로도 행이 유일한** 것을 찾는다.

    웨어하우스 차원은 기본 키에 계층 단계를 함께 넣는 일이 흔한데(``D_ITEM`` 의
    ``(ITEM_GROUP_CD, ITEM_CD)``), 팩트는 말단 코드만 들고 있어서 전체 키가 맞지
    않는다. 그 부분 키로도 유일하면 참조 대상이 될 수 있다 — 유일성은 데이터로만
    확인되므로 여기서 센다.

    비용은 두 가지로 묶는다. 한 표의 모든 PK 컬럼을 **질의 한 번**에 센다 —
    ``COUNT(*)`` 도 같이 나오므로 컬럼마다 따로 세던 것과 결과가 같다. 행 수
    추정이 ``max_rows`` 를 넘는 표는 아예 건너뛴다.
    """
    found: dict[str, tuple[str, ...]] = {}
    for table in schema.tables:
        if len(table.primary_key) < 2:
            continue
        candidates = [
            column for column in table.primary_key if not _is_load_metadata(column)
        ]
        if not candidates:
            continue
        if (
            max_rows is not None
            and table.row_estimate is not None
            and table.row_estimate > max_rows
        ):
            continue

        exprs = ", ".join(f"COUNT(DISTINCT {quoted(c, dialect)})" for c in candidates)
        cursor.execute(
            f"SELECT COUNT(*), {exprs} "
            f"FROM {_table_ref(table.schema, table.name, dialect)}"
        )
        row = cursor.fetchone()
        if not row or len(row) < 1 + len(candidates):
            continue
        total = row[0]
        for i, column in enumerate(candidates, start=1):
            distinct = row[i] if row[i] is not None else 0
            if total and total == distinct:
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
    bridge_middles: bool = True,
    probe_values: bool = True,
    max_probes: int = DEFAULT_MAX_PROBES,
) -> tuple[PhysicalSchema, tuple[ForeignKey, ...]]:
    """기본 키로 관계를 짓고, 데이터로 걸러 붙인 스키마와 그 관계들.

    ``targets`` 는 참조 *대상* 이 될 수 있는 표를 제한한다. 비워 두면 나가는
    키가 없는 표(= 차원)부터 시작해 두 라운드로 넓힌다.

    두 번째 라운드(:func:`bridge_middles`)가 스노우플레이크를 잇는다. 첫 라운드의
    대상 제한("나가는 키가 없는 표")은 중간 차원을 못 잡는다 — ``F → D_MID →
    D_TOP`` 에서 ``D_MID`` 는 나가는 키가 있어서 빠진다. 구조만으로는 중간 차원과
    팩트를 가릴 수 없으므로 여기서는 데이터가 가르게 한다: 대상을 기본 키를 가진
    모든 표로 넓히되, 통과한 엣지를 **형제 가드** 로 다시 검사한다.

    형제 가드: 자식과 부보가 *이미 공유하는 조상* 이 있으면 그 엣지는 버린다.
    팩트 둘이 같은 차원 코드를 들고 있으면 값 포함도 위반율도 다 통과하지만,
    그건 관계가 아니라 사촌이다 — 둘 다 이미 ``D_ORG`` 에 닿아 있으므로 직접
    엣지는 아무것도 사 오지 않는다. 반대로 진짜 중간 차원은 아직 아무도 닿지
    않은 곳에 살아 있다. 드물게 진짜 관계도 같이 잘린다(두 경로가 다시 한 곳에서
    만나는 모양) — 가짜 조인이 조용히 행을 복제하는 것보다 낫다.

    ``probe_values`` 는 이름으로 설명되지 않는 컬럼까지 값을 센다
    (:mod:`tablefold.relate.discover`). ``CUST_ID`` → ``MEMBER_NO`` 처럼 이름이
    전혀 다른 관계가 이걸로 발견된다. 예산(``max_probes``)이 질의 수를 묶는다.

    ``targets`` 를 명시하면 호출자의 의도가 우선이다 — 두 라운드 확장 없이 그
    목록만 쓰고, 프로브와 형제 가드는 그대로 작동한다.
    """
    from tablefold.relate.discover import (
        infer_from_name_tokens,
        probe_relationships,
    )
    from tablefold.relate.graph import SchemaGraph
    from tablefold.relate.keys import infer_from_primary_keys

    subsets = unique_single_keys(schema, cursor, dialect=dialect)
    # 가상 테이블(합성·요약·파티션 결합)은 질의할 수 없다. 그 엣지는 만들 때부터
    # 의도된 것이므로 검증 대상이 아니라 사실이다.
    virtual = {t.name.lower() for t in schema.tables if t.is_virtual}

    def candidates_for(round_targets: tuple[str, ...]) -> tuple[ForeignKey, ...]:
        exact = infer_from_primary_keys(
            schema,
            targets=tuple(t for t in round_targets if t.lower() not in virtual),
            unique_subsets=subsets,
        )
        # 토큰 후보는 정확 일치가 놓친 것만 남긴다. 같은 쌍이라면 정확 일치의
        # 확신도(0.9)가 어휘소 추정(0.6)보다 낫다.
        claimed = {
            (fk.from_table.lower(), tuple(c.lower() for c in fk.from_columns))
            for fk in exact
        }
        fuzzy = [
            fk
            for fk in infer_from_name_tokens(schema, targets=round_targets)
            if (fk.from_table.lower(), tuple(c.lower() for c in fk.from_columns))
            not in claimed
        ]
        merged = exact + tuple(fuzzy)
        return tuple(
            fk
            for fk in merged
            if fk.from_table.lower() not in virtual
            and fk.to_table.lower() not in virtual
        )

    if targets is None:
        graph = SchemaGraph.build(schema)
        targets = tuple(
            t.name
            for t in schema.tables
            if not graph.out_degree(t.name) and t.primary_key
        )
        second_round = bridge_middles and bool(targets)
    else:
        second_round = False

    accepted: list[ForeignKey] = []
    seen: set[tuple[str, str, str]] = {
        (
            fk.from_table.lower(),
            tuple(c.lower() for c in fk.from_columns),
            fk.to_table.lower(),
        )
        for fk in schema.foreign_keys
    }

    # 형제 가드의 캐시. 받아들인 엣지가 늘어 그래프가 바뀌었을 때만 다시 짓고,
    # 같은 그래프에서는 표별 도달 집합도 재사용한다.
    guard_cache: dict[str, object] = {"n": -1}

    def is_sibling_noise(candidate: ForeignKey) -> bool:
        """자식과 후보 부모가 이미 공유 조상을 갖는가.

        선언된 키와 지금까지 받아들인 엣지로 도달 집합을 계산한다. 자식이 이미
        부모의 세계에 닿아 있으면(부모 자신, 또는 부모가 닿는 어느 표든) 이
        엣지는 새로운 것을 사 오지 않는다 — 남는 것은 사촌 엣지나 순환뿐이다.
        """
        if guard_cache["n"] != len(accepted):
            fks = schema.foreign_keys + tuple(accepted)
            built = SchemaGraph.build(schema.with_foreign_keys(fks))
            guard_cache["graph"] = built
            guard_cache["reach"] = {}
            guard_cache["n"] = len(accepted)
        graph_built: SchemaGraph = guard_cache["graph"]  # type: ignore[assignment]
        reach_map: dict[str, set[str]] = guard_cache["reach"]  # type: ignore[assignment]

        def reach(table: str) -> set[str]:
            key = table.lower()
            if key not in reach_map:
                reach_map[key] = {
                    name.lower()
                    for name, _ in graph_built.walk_many_to_one(table, max_hops=8)
                }
            return reach_map[key]

        child = reach(candidate.from_table)
        parent_key = candidate.to_table.lower()
        return parent_key in child or bool(child & reach(candidate.to_table))

    def admit(candidates: tuple[ForeignKey, ...], *, guard: bool) -> int:
        added = 0
        for candidate in candidates:
            mark = (
                candidate.from_table.lower(),
                candidate.from_columns[0].lower(),
                candidate.to_table.lower(),
            )
            if mark in seen:
                continue
            rate = violation_rate(candidate, cursor, dialect=dialect)
            if rate > tolerance:
                continue
            measured = replace(candidate, confidence=round(1.0 - rate, 4))
            if guard and is_sibling_noise(candidate):
                continue
            seen.add(mark)
            accepted.append(measured)
            added += 1
        return added

    admit(candidates_for(targets), guard=False)

    if second_round:
        wide_targets = tuple(t.name for t in schema.tables if t.primary_key)
        admit(candidates_for(wide_targets), guard=True)

    if probe_values:
        working = schema.with_foreign_keys(schema.foreign_keys + tuple(accepted))
        # 프로브 후보도 형제 가드를 거친다. 팩트끼리 공유하는 코드 영역은 값
        # 포함을 통과하지만, 그것은 관계가 아니라 사촌이다.
        probed = []
        for fk in probe_relationships(
            working, cursor, dialect=dialect, max_probes=max_probes
        ):
            if not is_sibling_noise(fk):
                probed.append(fk)
        accepted.extend(probed)

    if not accepted:
        return schema, ()
    merged = schema.with_foreign_keys(schema.foreign_keys + tuple(accepted))
    return merged, tuple(accepted)


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
