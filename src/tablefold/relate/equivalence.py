"""같은 정보를 담는 컬럼을 찾아 하나로 세운다 — 손실 없는 압축.

웨어하우스 차원 테이블을 열면 같은 내용의 다른 철자가 득실거린다. ``ORG_ID`` 와
``ORG_CD`` 는 행마다 같이 움직이고, ``STATUS`` 와 ``STATUS_NM`` 을 나란히 두는
이유는 없다. 이런 쌍은 **상호 함수적 종속**(a→b 이고 b→a)으로 확인되며, 확인만
되면 하나를 지워도 아무것도 잃지 않는다 — 필요하면 대표값에서 되찾을 수 있으니
손실이 아니라 별칭이다.

판정은 이름이 아니라 값으로 한다. 세 개의 수를 센다:

* ``COUNT(DISTINCT a)``, ``COUNT(DISTINCT b)`` — 각자의 폭
* ``(a, b) 짝의 distinct 수``

짝의 수가 둘 다와 같으면 a↔b 는 동치다. 한 방향만 같으면(a→b 만 성립) 대표
한쪽을 남기는 게 아니라 **그냥 놓는다** — 계층(PK → 속성)은 정합 설계다. 여기서
줄어드는 것은 우연히 중복된 것뿐이다.

후보를 줄이는 장치도 데이터에서 온다: distinct 수가 다른 쌍은 시험조차 하지
않는다(동치일 수가 없다). 카디널리티 일람은 표당 질의 한 번으로 읽는다.

여기서 나온 묶음을 레이어에 적용하면(:func:`dedupe_fields`) 모델당 필드가
진짜로 줄어든다. 프롬프트가 짧아지고, 반영도는 통합된 컬럼을 "잃음"이 아니라
"동치 통합"으로 따로 세어 거짓 저하는 막는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.choose.cost import is_noise
from tablefold.ir import LogicalLayer, PhysicalSchema, PhysicalTable
from tablefold.relate.discover import _strip_key_suffix
from tablefold.relate.validate import Cursor


@dataclass(frozen=True)
class EquivalentGroup:
    """하나로 세울 수 있었던 컬럼들. 첫 번째가 대표다."""

    table: str
    columns: tuple[str, ...]
    """대표 먼저. 나머지는 별칭."""


# 이 행수 아래에서는 동치를 인정하지 않는다. 두 행짜리 표에서 임의의 두 컬럼은
# 항상 서로를 결정한다 — 정의상 맞지만 근거가 없는 압축이다. 견본이 얇으면
# "몰라서 안 자른다"가 정답이다.
MIN_ROWS = 8


def find_equivalents(
    schema: PhysicalSchema,
    cursor: Cursor,
    *,
    dialect: str = "tsql",
) -> tuple[EquivalentGroup, ...]:
    """같은 표 안에서 값이 항상 함께 결정되는 컬럼 묶음을 찾는다.

    표 단위로 끊는 이유는 단순함 때문만이 아니다. 다른 표 사이의 같은 코드는
    참조 관계의 흔적이지 중복이 아니다 — 그건 fold 가 이미 조인으로 처리한다.
    """
    groups: list[EquivalentGroup] = []
    for table in schema.tables:
        if table.is_virtual or len(table.columns) < 2:
            continue
        if _row_count(table, cursor, dialect) < MIN_ROWS:
            continue
        # 후보는 **키 같은 컬럼**만으로 좁힌다. ``ORG_ID`` 와 ``ORG_NM`` 이
        # 데이터상 동치여도 이름을 지우면 "조직 이름은?" 질문이 답을 잃는다 —
        # 정보량은 같아도 쓰임이 다르다. 식별자 복제(ID↔CD, CODE↔NO)만이
        # 지워도 아무도 울지 않는 자리다. 기본 키 멤버도 후보다 — ``ORG_ID``
        # 를 지우고 ``ORG_CD`` 를 남기는 것이 이 모듈의 대표 사례다.
        names = [
            c.name
            for c in table.columns
            if not is_noise(c.name) and _strip_key_suffix(c.name.lower()) is not None
        ]
        if len(names) < 2:
            continue
        widths = _distinct_widths(table, names, cursor, dialect)
        # 폭이 같은 것끼리만 시험한다. 폭이 다르면 동치일 가능성 자체가 없다.
        by_width: dict[int, list[str]] = {}
        for name in names:
            width = widths.get(name.lower())
            if width is not None:
                by_width.setdefault(width, []).append(name)

        claimed: set[str] = set()
        for width in sorted(by_width):
            cohort = [n for n in by_width[width] if n.lower() not in claimed]
            for i, left in enumerate(cohort):
                if left.lower() in claimed:
                    continue
                members = [left]
                for right in cohort[i + 1 :]:
                    if _mutually_determines(
                        table,
                        (members[0], right),
                        cursor,
                        dialect,
                    ):
                        members.append(right)
                        claimed.add(right.lower())
                if len(members) > 1:
                    claimed.add(members[0].lower())
                    groups.append(EquivalentGroup(table.name, tuple(members)))
    return tuple(groups)


def dedupe_fields(
    layer: LogicalLayer,
    groups: tuple[EquivalentGroup, ...],
) -> tuple[LogicalLayer, int]:
    """레이어 필드에서 별칭을 빼고, 대표 필드의 설명에 남긴다.

    지우기만 하면 LLM 이 별칭으로 물었을 때 답을 못 찾는다. 그래서 대표 필드
    설명에 "``X`` 도 같은 값"을 적는다 — 프롬프트 한 줄로 별칭까지 커버한다.
    """
    alias_to_rep: dict[tuple[str, str], str] = {}
    for group in groups:
        rep = group.columns[0].lower()
        for alias in group.columns[1:]:
            alias_to_rep[(group.table.lower(), alias.lower())] = rep

    if not alias_to_rep:
        return layer, 0

    removed = 0
    models = []
    for model in layer.models:
        kept = []
        merged_notes: dict[str, list[str]] = {}
        for f in model.fields:
            key = (f.source.table.lower(), f.source.column.lower())
            rep = alias_to_rep.get(key)
            if rep is None:
                kept.append(f)
                continue
            removed += 1
            merged_notes.setdefault(rep, []).append(f.name)

        new_fields = []
        for f in kept:
            notes = merged_notes.get(f.source.column.lower())
            if notes:
                suffix = " · ".join(sorted(notes))
                hint = f"{f.source.column} 과 같은 값을 묻는 이름: {suffix}"
                from dataclasses import replace as _r

                f = _r(f, description=hint)
            new_fields.append(f)

        from dataclasses import replace as _r

        models.append(_r(model, fields=tuple(new_fields)))

    from dataclasses import replace as _r

    notes = (*layer.notes, f"동치 컬럼 통합: 별칭 {removed}개를 대표 값으로 접음")
    return _r(layer, models=tuple(models), notes=notes), removed


# ── 내부 ─────────────────────────────────────────────────────────────────────


def _row_count(table: PhysicalTable, cursor: Cursor, dialect: str) -> int:
    from tablefold.relate.validate import quoted

    try:
        cursor.execute(f"SELECT COUNT(*) FROM {quoted(table.name, dialect)}")
        row = cursor.fetchone()
    except Exception:  # noqa: BLE001
        return 0
    return int(row[0]) if row and row[0] else 0


def _distinct_widths(
    table: PhysicalTable,
    names: list[str],
    cursor: Cursor,
    dialect: str,
) -> dict[str, int | None]:
    from tablefold.relate.validate import quoted

    ref = quoted(table.name, dialect)
    exprs = ", ".join(f"COUNT(DISTINCT {quoted(n, dialect)})" for n in names)
    try:
        cursor.execute(f"SELECT {exprs} FROM {ref}")
        row = cursor.fetchone()
    except Exception:  # noqa: BLE001
        return {n.lower(): None for n in names}
    if row is None:
        return {n.lower(): None for n in names}
    out: dict[str, int | None] = {}
    for i, n in enumerate(names):
        value = row[i] if i < len(row) else None
        out[n.lower()] = int(value) if value is not None else None
    return out


def _mutually_determines(
    table: PhysicalTable,
    pair: tuple[str, str],
    cursor: Cursor,
    dialect: str,
) -> bool:
    """``(a,b) 짝의 distinct 수 == a 의 == b 의`` 면 동치다.

    NULL 은 DISTINCT 가 하나의 값으로 묶으므로 자연히 제외된다 — 값이 없는 것과
    같은 값을 가진 것을 섞지 않는다는 원칙의 연장이다.
    """
    from tablefold.relate.validate import quoted

    a, b = pair
    ref = quoted(table.name, dialect)
    qa, qb = quoted(a, dialect), quoted(b, dialect)
    try:
        cursor.execute(
            f"SELECT "
            f"(SELECT COUNT(*) FROM (SELECT DISTINCT {qa}, {qb} FROM {ref}) d), "
            f"(SELECT COUNT(DISTINCT {qa}) FROM {ref}), "
            f"(SELECT COUNT(DISTINCT {qb}) FROM {ref})"
        )
        row = cursor.fetchone()
    except Exception:  # noqa: BLE001 — 판정 실패는 통과가 아니라 보류다
        return False
    if not row or any(v is None for v in row):
        return False
    pairs, wa, wb = int(row[0]), int(row[1]), int(row[2])
    return pairs == wa == wb and pairs > 0
