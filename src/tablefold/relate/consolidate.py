"""월별 스냅샷으로 쪼개 놓은 원장을 한 표로 되돌려 놓는다.

원장 테이블을 월 단위로 스냅샷 떠서 ``F_LEDGER_202506`` · ``F_LEDGER_202507`` …
로 쌓아 두는 관례가 흔하다. 폴드는 이걸 압축하지 못했다 — 스냅샷끼리는 서로를
참조할 리 없으니 그래프에 간선이 없고, 간선이 없으면 모델도 따로 놀았다.
12개월치면 같은 질문이 13개(원장+스냅샷) 모델로 갈라진다.

여기서는 **구조가 완전히 같고** 이름 끝에 기간 접미사(``YYYYMMDD`` · ``YYYYMM``
· ``YYYY``)가 붙은 표들을 한 가상 테이블로 합친다. 합치는 수단은
:attr:`tablefold.ir.PhysicalTable.source_sql` 의 ``UNION ALL`` — 확장기가 이미
가상 테이블을 서브쿼리로 펼치는 길(:func:`tablefold.rewrite.expand`)이 있으므로
물리 계층은 손대지 않는다.

판단 근거가 둘이다. 컬럼 *시그니처*(이름+타입, 순서까지)가 정확히 같아야 하고,
최소 둘은 되어야 한다. 하나라도 어긋나면 합치지 않는다 — 우연히 비슷한 표를
합쳐 행을 지어내는 것보다 못 합치는 게 낫다.

합친 표에는 기간 판별 컬럼을 붙인다. 어느 스냅샷에서 왔는지 몰라서는 "7월만"
같은 질문이 다시 불가능해지므로, 접미사 값을 문자열 리터럴로 싣는다. 기본 키는
버린다 — 아이디가 월을 넘어 유일하다고 장담할 수 없고, 유일성을 거짓으로
주장하면 나중에 참조 대상으로 오용된다.

엣지는 **모든 멤버가 같은 곳을 가리킬 때만** 살린다. 2024년 스냅샷만 옛 차원을
가리키는 식으로 엇갈리면, 한쪽의 관계를 전체의 관계로 넘기지 않고 버린다.

``UNION`` 이 아니라 ``UNION ALL`` 인 이유는 기간 앵커
(:func:`tablefold.relate.synthesize`)와 반대다. 앵커는 키당 한 행이어야 하지만
스냅샷은 행이 쌓이는 것이 사실이다 — 중복 제거는 답을 지어내는 일이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable

# 최소 멤버 수. 혼자 있는 "스냅샷"은 스냅샷이 아니라 그냥 표다.
MIN_MEMBERS = 2

# (접미사 규칙, 종류, 판별 컬럼 이름). 연도는 상식 범위로 걸러고 월·일 자리는
# 자릿수로만 본다 — 시그니처가 같다는 조건이 이미 절반을 걸러 준다.
_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^(?P<stem>.+)_(?P<value>\d{8})$"), "day", "SNAPSHOT_YYYYMMDD"),
    (re.compile(r"^(?P<stem>.+)_(?P<value>\d{6})$"), "month", "SNAPSHOT_YYYYMM"),
    (
        re.compile(r"^(?P<stem>.+)_(?P<value>(?:18|19|20)\d{2})$"),
        "year",
        "SNAPSHOT_YYYY",
    ),
)


@dataclass(frozen=True)
class Consolidation:
    """한 벌로 합쳐진 스냅샷 묶음과 무엇이 버려졌는지.

    읽는 쪽은 버려진 엣지 수를 보고 판단해야 한다 — 조용히 사라진 관계는 나중에
    "왜 이 모델에는 조인이 없지?"라는 질문으로 돌아온다.
    """

    virtual: PhysicalTable
    members: tuple[str, ...]
    unified_edges: int = 0
    """모든 멤버가 같은 곳을 가리켜 살아남은 엣지 수."""
    dropped_outgoing_edges: int = 0
    """멤버끼리 가리키는 곳이 엇갈려 버린 엣지 수."""
    dropped_incoming_edges: int = 0
    """멤버를 참조하던 엣지 수. 스냅샷을 참조하는 표는 드물고, 누가 참조했는지에
    따라 의미가 달라져 여기서 추정하지 않는다."""


def partition_of(table_name: str) -> tuple[str, str, str] | None:
    """``F_LEDGER_202506`` → ``("F_LEDGER", "month", "202506")``.

    숫자만 붙은 이름(``_12345``)은 기간이 아니라 그냥 번호일 수 있어 받지
    않는다.
    """
    for pattern, kind, _ in _PERIOD_PATTERNS:
        match = pattern.match(table_name)
        if match:
            return match.group("stem"), kind, match.group("value")
    return None


def consolidate_snapshots(
    schema: PhysicalSchema,
) -> tuple[PhysicalSchema, tuple[Consolidation, ...]]:
    """구조가 같은 기간 파티션 표를 가상 테이블 한 벌로 합친 스키마.

    합쳐진 표의 이름은 원본에서 접미사를 뗀 것(``F_LEDGER_202506`` 들 →
    ``F_LEDGER``)이다. 그 이름이 이미 살아 있는 표가 쓰고 있으면 ``_ALL`` 을
    붙여 물러난다 — 원본을 덮는 편이 합치는 것보다 나쁘다.
    """
    groups: dict[tuple, list[PhysicalTable]] = {}
    for table in schema.tables:
        parts = partition_of(table.name)
        if parts is None or table.is_virtual:
            continue
        stem, kind, _ = parts
        signature = (stem, kind, _signature(table))
        groups.setdefault(signature, []).append(table)

    taken = {t.name.lower() for t in schema.tables}
    drafts: list[tuple[str, PhysicalTable, list[PhysicalTable]]] = []
    for (stem, _, _), members in sorted(groups.items()):
        if len(members) < MIN_MEMBERS:
            continue
        ordered = sorted(members, key=lambda t: t.name.lower())
        name = stem if stem.lower() not in taken else f"{stem}_ALL"
        while name.lower() in taken:
            name = f"{name}_X"
        taken.add(name.lower())

        kind = partition_of(ordered[0].name)[1]
        disc_name = next(d for _, k, d in _PERIOD_PATTERNS if k == kind)

        columns = (
            *ordered[0].columns,
            PhysicalColumn(
                name=disc_name,
                type=f"varchar({len(partition_of(ordered[0].name)[2])})",
                comment="스냅샷 기간 — 표 이름 접미사에서 뗀 값",
            ),
        )

        virtual = PhysicalTable(
            name=name,
            columns=columns,
            primary_key=(),
            row_estimate=_sum_rows(ordered),
            comment=(
                f"기간 파티션 결합 — {len(ordered)}개 스냅샷 "
                f"({ordered[0].name} … {ordered[-1].name})"
            ),
            source_sql=_union_sql(ordered, disc_name),
        )
        drafts.append((name, virtual, ordered))

    if not drafts:
        return schema, ()

    return _rebuild(schema, drafts)


# ── 내부 ─────────────────────────────────────────────────────────────────────


def _signature(table: PhysicalTable) -> tuple[tuple[str, str], ...]:
    from tablefold.relate.graph import _type_class

    return tuple((c.name.lower(), _type_class(c.type)) for c in table.columns)


def _sum_rows(members: list[PhysicalTable]) -> int | None:
    estimates = [m.row_estimate for m in members]
    if any(e is None for e in estimates):
        return None
    return sum(int(e) for e in estimates)


def _union_sql(members: list[PhysicalTable], disc_name: str) -> str:
    """멤버 SELECT 에 기간 리터럴을 얹어 UNION ALL 로 잇는다.

    식별자를 인용하지 않는다 — 카탈로그 이름이라 공백이 없고, 이 SQL 을 sqlglot
    이 방언 중립으로 파싱한 뒤 대상 방언으로 다시 쓴다.
    """
    parts: list[str] = []
    for member in members:
        value = partition_of(member.name)[2]
        cols = ", ".join(c.name for c in member.columns)
        parts.append(
            f"SELECT {cols}, '{value}' AS {disc_name} FROM {member.qualified_name}"
        )
    return "\nUNION ALL\n".join(parts)


def _unified_edges(
    schema: PhysicalSchema,
    members: list[PhysicalTable],
) -> tuple[set[tuple], int]:
    """모든 멤버가 같게 가리키는 엣지 표식과 버려진 수.

    엣지를 비교할 때 목적지와 컬럼·조건 전부를 본다. 하나라도 엇갈리면 그
    목적지는 포기한다 — 절반의 진실을 전체의 진실처럼 넘기지 않는다.
    """
    per_member: list[set[tuple]] = []
    for member in members:
        marks = {
            _mark_of(fk)
            for fk in schema.foreign_keys
            if fk.from_table.lower() == member.name.lower()
        }
        per_member.append(marks)

    common = set.intersection(*per_member) if per_member else set()
    total = sum(len(marks) for marks in per_member)
    return common, total - len(common) * len(members)


def _rebuild(
    schema: PhysicalSchema,
    drafts: list[tuple[str, PhysicalTable, list[PhysicalTable]]],
) -> tuple[PhysicalSchema, tuple[Consolidation, ...]]:
    """멤버 표를 빼고 가상 표를 넣고, 엣지를 다시 잇고, 장부를 남긴다."""
    from dataclasses import replace as _dc_replace

    members = {t.name.lower() for _, _, ms in drafts for t in ms}
    owner_of = {t.name.lower(): name for name, _, ms in drafts for t in ms}

    tables = [t for t in schema.tables if t.name.lower() not in members]
    tables += [virtual for _, virtual, _ in drafts]

    kept_fks: list[ForeignKey] = []
    incoming_by_virtual: dict[str, int] = {}
    for fk in schema.foreign_keys:
        src = fk.from_table.lower()
        dst = fk.to_table.lower()
        if src in members and dst in members:
            continue  # 스냅샷끼리의 엣지는 없었다고 본다
        if src in members:
            continue  # 통합 엣지는 아래에서 다시 만든다
        if dst in members:
            # 멤버를 참조하던 엣지는 가상 표로 옮기지 않는다. 어느 달을
            # 참조했느냐가 의미를 바꾸는 경우가 많다 — 조용히 넓히지 않는다.
            owner = owner_of.get(dst, dst)
            incoming_by_virtual[owner] = incoming_by_virtual.get(owner, 0) + 1
            continue
        kept_fks.append(fk)

    final_fks = list(kept_fks)
    finished: list[Consolidation] = []
    for name, virtual, member_tables in drafts:
        common_marks, dropped = _unified_edges(schema, member_tables)
        by_mark: dict[tuple, list[ForeignKey]] = {}
        for fk in schema.foreign_keys:
            if fk.from_table.lower() in {m.name.lower() for m in member_tables}:
                by_mark.setdefault(_mark_of(fk), []).append(fk)

        for mark in sorted(common_marks):
            candidates = by_mark[mark]
            representative = min(candidates, key=lambda fk: fk.from_table.lower())
            final_fks.append(
                ForeignKey(
                    from_table=name,
                    from_columns=representative.from_columns,
                    to_table=representative.to_table,
                    to_columns=representative.to_columns,
                    inferred=True,
                    confidence=min(fk.confidence for fk in candidates),
                    key_expressions=representative.key_expressions,
                    condition=representative.condition,
                )
            )
        finished.append(
            _dc_replace(
                Consolidation(
                    virtual=virtual,
                    members=tuple(m.name for m in member_tables),
                ),
                unified_edges=len(common_marks),
                dropped_outgoing_edges=dropped,
                dropped_incoming_edges=incoming_by_virtual.get(name, 0),
            )
        )

    return (
        PhysicalSchema(tables=tuple(tables), foreign_keys=tuple(final_fks)),
        tuple(finished),
    )


def _mark_of(fk: ForeignKey) -> tuple:
    return (
        tuple(c.lower() for c in fk.from_columns),
        fk.to_table.lower(),
        tuple(c.lower() for c in fk.to_columns),
        fk.condition,
        fk.key_expressions,
    )
