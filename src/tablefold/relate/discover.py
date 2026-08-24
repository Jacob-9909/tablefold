"""이름이 말해 주지 않는 관계를 찾는다.

기존 복구는 두 가지 이름 규칙에 기댄다 — 컬럼 접미사 어간이 테이블 별칭과
맞아야 하고(:func:`tablefold.relate.graph.infer_foreign_keys`), 참조 키 컬럼
이름이 대상 기본 키와 *정확히* 같아야 한다
(:func:`tablefold.relate.keys.infer_from_primary_keys`). 실제 웨어하우스는 이
둘 다 어긋나는 경우가 흔하다.

* ``telecom_card_cb_combined_info.CUST_ID`` → ``member_info.MEMBER_NO`` —
  어간 ``cust`` 는 테이블 이름 어디에도 없고, 컬럼 이름도 대상 키와 다르다.
* ``F_SALES.SA_ORG_CD`` → ``D_SA_ORG.ORG_CD`` — 한정 접두사 ``SA_`` 가 붙어
  정확 일치가 실패한다.

여기서는 두 갈래로 넓힌다.

**토큰 정합** — 스키마만 본다. 컬럼 이름을 어휘소로 쪼개 대상 테이블·키 이름의
어휘소가 전부 덮는지 본다. 데이터를 읽지 않으므로 "가능한" 후보일 뿐이고,
확신도는 낮게 잡는다. 데이터 경로에서는 위반율 검증을 반드시 거친다.

**값 포함 탐사** — 이름이 아무것도 말해 주지 않을 때 값을 센다. 참조 관계라는
것은 "자식의 구별되는 값이 부모 키 값 안에 들어간다"는 사실이므로, 타입이 맞는
키-컬럼 쌍을 후보로 만들어 실제로 세어 본다. 발견된 엣지의 확신도는 자리표시자가
아니라 관측값이다.

탐사 비용은 예산으로 묶는다. 후보 전체를 다 세면 스키마가 클 때 질의 수가
후보 수에 비례해서 늘어난다. 이름 유사도 순으로 줄을 세워 예산만큼만 probe
한다 — 가장 그럴듯한 쌍부터 확인하므로 예산이 잘릴 때 손해가 작다.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable
from tablefold.relate.graph import _KEY_SUFFIXES, _type_class
from tablefold.relate.validate import Cursor, quoted

# 그래프 모듈의 접미사에 웨어하우스 관례를 더한다. ``_CD`` 는 국내 창고 스키마에서
# 가장 흔한 키 꼬리다 — ``ORG_CD`` · ``ITEM_CD`` · ``ACCT_CD``. 이게 빠지면 어간
# 추출이 아예 시작되지 않으므로 여기서 확장해 쓴다.
_KEY_LIKE_SUFFIXES = _KEY_SUFFIXES + ("_cd",)


def _strip_key_suffix(column_name: str) -> str | None:
    for suffix in _KEY_LIKE_SUFFIXES:
        if column_name.endswith(suffix) and len(column_name) > len(suffix):
            return column_name[: -len(suffix)]
    return None


# 프로브 질의 상한. 후보는 유사도 순이라 잘려도 그럴듯한 쪽이 먼저 확인된다.
DEFAULT_MAX_PROBES = 24

# 어느 표에나 있으면서 관계를 뜻하지 않는 적재 메타 컬럼.
_LOAD_METADATA = frozenset({"load_dt", "load_user", "etl_id", "etl_dt", "etl_job_id"})

# 다형 참조로 흔한 이름. 값 포함이 우연히 통과할 확률이 높고(모든 표의 id 집합은
# 서로 겹치기 쉽다) 의미도 모호해서 탐사 후보에서 뺀다.
_POLYMORPHIC = frozenset(
    {"id", "parent_id", "actor_id", "entity_id", "user_id", "owner_id", "ref_id"}
)


@dataclass(frozen=True)
class Candidate:
    """값 포함 탐사의 후보 쌍. ``score`` 가 높을수록 이름이 그럴듯하다."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    score: float


# ── 어휘소 ────────────────────────────────────────────────────────────────────


def tokens(name: str) -> tuple[str, ...]:
    """이름을 `_` 경계로 쪼갠 어휘소. 빈 조각과 숫자 꼬리는 버린다."""
    return tuple(t for t in name.lower().split("_") if t and not t.isdigit())


def _token_eq(a: str, b: str) -> bool:
    """두 어휘소가 같은 낱말을 가리킨다고 본다.

    웨어하우스 약어를 다루기 위한 완화다: ``org`` 와 ``organization`` 처럼 짧은
    쪽이 긴 쪽의 접두사면 같은 것으로 본다. 2글자 이하는 우연 접두사가 너무
    흔하다(``sa``/``sample``) — 정확히 같을 때만 받는다.
    """
    if a == b:
        return True
    return len(a) >= 3 and (b.startswith(a) or a.startswith(b))


def coverage(stem: str, vocabulary: frozenset[str] | set[str]) -> float:
    """컬럼 어간 어휘소 중 대상 어휘가 덮는 비율. 1.0이면 전부 덮인다."""
    parts = tokens(stem)
    if not parts:
        return 0.0
    hit = sum(1 for p in parts if any(_token_eq(p, v) for v in vocabulary))
    return hit / len(parts)


def _target_vocabulary(table_name: str, key_column: str) -> frozenset[str]:
    return frozenset(tokens(table_name) + tokens(key_column))


# ── 토큰 정합 (스키마만) ──────────────────────────────────────────────────────


def infer_from_name_tokens(
    schema: PhysicalSchema,
    *,
    targets: tuple[str, ...] | None = None,
    min_score: float = 1.0,
    confidence: float = 0.6,
) -> tuple[ForeignKey, ...]:
    """어간 어휘소가 대상 테이블·키 어휘에 **전부** 들어맞는 단일 키 참조를 찾는다.

    정확 일치(:func:`tablefold.relate.keys.infer_from_primary_keys`)의 일반화다.
    ``SA_ORG_CD`` → ``D_SA_ORG.ORG_CD`` 는 정확 일치로는 못 찾지만 어휘소로는
    찾는다. 데이터를 읽지 않으므로 기본 ``min_score=1.0`` — 어간 하나까지 전부
    설명되지 않으면 후보조차 만들지 않는다. 낮추면 부분 일치도 받지만, 그건
    데이터 검증과 함께 쓸 때만 안전하다.

    ``targets`` 는 참조 *대상* 제한이다. 주지 않으면 모든 표가 대상이라 팩트의
    공유 코드(``ORG_CD``)끼리 서로를 가리키는 엣지가 이름만 맞으면 만들어진다.
    이름 규칙보다 느슨한 만큼 제한도 더 중요하다.
    """
    explained = {
        (fk.from_table.lower(), tuple(c.lower() for c in fk.from_columns))
        for fk in schema.foreign_keys
    }
    allowed = {t.lower() for t in targets} if targets is not None else None
    pk_of: dict[str, str] = {}
    for table in schema.tables:
        if len(table.primary_key) != 1:
            continue
        key_column = table.column(table.primary_key[0])
        if key_column is not None:
            pk_of[table.name.lower()] = key_column.name

    found: list[ForeignKey] = []
    seen: set[tuple[str, str, str]] = set()
    for table in schema.tables:
        for column in table.columns:
            lowered = column.name.lower()
            if lowered in {c.lower() for c in table.primary_key}:
                continue
            if lowered in _POLYMORPHIC or lowered in _LOAD_METADATA:
                continue
            stem = _strip_key_suffix(lowered)
            if stem is None:
                continue
            if (table.name.lower(), (lowered,)) in explained:
                continue

            best: tuple[float, PhysicalTable, str] | None = None
            for target in schema.tables:
                if target.name.lower() == table.name.lower():
                    continue
                if allowed is not None and target.name.lower() not in allowed:
                    continue
                key_column_name = pk_of.get(target.name.lower())
                if key_column_name is None:
                    continue
                key_column = target.column(key_column_name)
                if key_column is None:
                    continue
                if _type_class(column.type) != _type_class(key_column.type):
                    continue
                score = coverage(stem, _target_vocabulary(target.name, key_column_name))
                if score < min_score:
                    continue
                # 동점은 표 이름순 — 스키마 순서가 뒤바려도 같은 답.
                tiebreak = (target.name.lower(), key_column_name.lower())
                current_tiebreak = (
                    (best[1].name.lower(), best[2].lower()) if best else None
                )
                if (
                    best is None
                    or score > best[0]
                    or (score == best[0] and tiebreak < current_tiebreak)
                ):
                    best = (score, target, key_column_name)

            if best is None:
                continue
            _, target, key_column = best
            mark = (table.name.lower(), lowered, target.name.lower())
            if mark in seen:
                continue
            seen.add(mark)
            found.append(
                ForeignKey(
                    from_table=table.name,
                    from_columns=(column.name,),
                    to_table=target.name,
                    to_columns=(key_column,),
                    inferred=True,
                    confidence=confidence,
                )
            )
    return tuple(found)


# ── 값 포함 탐사 (데이터) ─────────────────────────────────────────────────────


def value_candidates(schema: PhysicalSchema) -> tuple[Candidate, ...]:
    """설명되지 않은 키 같은 컬럼 × 타입이 맞는 단일 키 대상의 후보 쌍.

    이미 선언·복구된 엣지가 차지한 (표, 컬럼)은 다시 만들지 않는다. 점수는
    어간 어휘가 대상 어휘를 덮는 비율 — 프로브 순서의 근거이지 통과 기준이
    아니다. 통과 기준은 값이다.
    """
    explained = {
        (fk.from_table.lower(), tuple(c.lower() for c in fk.from_columns))
        for fk in schema.foreign_keys
    }
    targets: list[tuple[PhysicalTable, PhysicalColumn]] = []
    for table in schema.tables:
        if table.is_virtual or len(table.primary_key) != 1:
            continue
        column = table.column(table.primary_key[0])
        if column is None:
            continue
        targets.append((table, column))

    found: list[Candidate] = []
    for table in schema.tables:
        # 값 탐사는 물리 표만 대상으로 한다 — 가상 표는 질의할 수 없다.
        if table.is_virtual:
            continue
        for column in table.columns:
            lowered = column.name.lower()
            if lowered in {c.lower() for c in table.primary_key}:
                continue
            if lowered in _POLYMORPHIC or lowered in _LOAD_METADATA:
                continue
            stem = _strip_key_suffix(lowered)
            if stem is None and not any(
                other.primary_key and lowered == other.primary_key[0].lower()
                for other in schema.tables
            ):
                # 접미사도 없고 남의 기본 키 이름과도 같지 않으면 참조 컬럼일
                # 근거가 약하다. 값 포함이 우연히 성공하는 낭비 프로브를 줄인다.
                continue
            if (table.name.lower(), (lowered,)) in explained:
                continue

            type_class = _type_class(column.type)
            scored: list[Candidate] = []
            for target, key_column in targets:
                if target.name.lower() == table.name.lower():
                    continue
                if _type_class(key_column.type) != type_class:
                    continue
                vocab = _target_vocabulary(target.name, key_column.name)
                scored.append(
                    Candidate(
                        from_table=table.name,
                        from_column=column.name,
                        to_table=target.name,
                        to_column=key_column.name,
                        score=round(coverage(stem or lowered, vocab), 4),
                    )
                )
            found.extend(scored)

    # 유사도 내림차순, 동점은 이름순 — 같은 스키마면 항상 같은 순서.
    found.sort(key=lambda c: (-c.score, c.from_table.lower(), c.to_table.lower()))
    return tuple(found)


def containment_rate(
    candidate: Candidate, cursor: Cursor, *, dialect: str = "tsql"
) -> tuple[float, int]:
    """자식의 **구별되는 값** 중 부모 키에 없는 것의 비율과 총 개수.

    행 단위가 아니라 값 단위로 센다 — 팩트 백만 행이 같은 차원 코드 열 번을
    반복해도 포함 관계의 증거 값은 열 개뿐이다. NULL 은 후보에서 뺀다. 값이
    하나도 없으면 (1.0, 0): 근거 없는 관계는 통과시키지 않는다.
    """
    src_col = quoted(candidate.from_column, dialect)
    tgt_col = quoted(candidate.to_column, dialect)

    def ref(name: str) -> str:
        return quoted(name, dialect)

    cursor.execute(
        f"SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN NOT EXISTS ("
        f"SELECT 1 FROM {ref(candidate.to_table)} t WHERE t.{tgt_col} = d.v"
        f") THEN 1 ELSE 0 END) AS orphans "
        f"FROM (SELECT DISTINCT s.{src_col} AS v "
        f"FROM {ref(candidate.from_table)} s "
        f"WHERE s.{src_col} IS NOT NULL) d"
    )
    row = cursor.fetchone()
    total = int(row[0]) if row and row[0] is not None else 0
    orphans = int(row[1]) if row and len(row) > 1 and row[1] is not None else total
    return (orphans / total if total else 1.0), total


def probe_relationships(
    schema: PhysicalSchema,
    cursor: Cursor,
    *,
    dialect: str = "tsql",
    max_probes: int = DEFAULT_MAX_PROBES,
    tolerance: float = 0.01,
) -> tuple[ForeignKey, ...]:
    """값 포함으로 관계를 찾는다. 통과 기준은 관측값, 예산은 질의 수.

    한 컬럼이 여러 대상을 통과하면 **위반율이 가장 낮은** 쪽 하나만 남긴다.
    하나의 컬럼이 두 부모를 참조하는 경우는 드물고, 나머지는 우연 포함이므로
    좁은 쪽이 참이다. 동점이면 이름 유사도가 높은 쪽.
    """
    candidates = value_candidates(schema)
    probed = 0
    results: dict[tuple[str, str], list[tuple[float, float, Candidate]]] = {}
    for candidate in candidates:
        if probed >= max_probes:
            break
        rate, total = containment_rate(candidate, cursor, dialect=dialect)
        probed += 1
        if total == 0 or rate > tolerance:
            continue
        mark = (candidate.from_table.lower(), candidate.from_column.lower())
        results.setdefault(mark, []).append((rate, -candidate.score, candidate))

    found: list[ForeignKey] = []
    for entries in results.values():
        # 위반율 → 유사도 → 이름 순. 세 번째까지 정해도 동점이면 어느 쪽이든
        # 참이라는 뜻이므로 이름순으로 정한다. Candidate 자체를 비교에 내보지
        # 않는 것은 데이터클래스 순서 비교가 정의되지 않았기 때문이다.
        rate, _, candidate = min(
            entries, key=lambda e: (e[0], e[1], e[2].to_table.lower())
        )
        found.append(
            ForeignKey(
                from_table=candidate.from_table,
                from_columns=(candidate.from_column,),
                to_table=candidate.to_table,
                to_columns=(candidate.to_column,),
                inferred=True,
                confidence=round(1.0 - rate, 4),
            )
        )
    return tuple(found)


__all__ = [
    "DEFAULT_MAX_PROBES",
    "Candidate",
    "containment_rate",
    "coverage",
    "infer_from_name_tokens",
    "probe_relationships",
    "tokens",
    "value_candidates",
]
