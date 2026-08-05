"""선언된 외래 키가 없는 스키마에서 기본 키만으로 관계를 복구한다.

데이터 웨어하우스는 외래 키 제약을 잘 걸지 않는다. 적재 성능을 깎고, ETL이 이미
정합성을 보장한다고 보기 때문이다. 그래서 스타 스키마인데도 ``sys.foreign_keys``
가 비어 있는 경우가 흔하다.

:func:`~tablefold.graph.graph.infer_foreign_keys` 는 이 상황을 컬럼 *이름* 으로
푼다 — ``customer_id`` 를 ``customers`` 로 되짚는 식이다. 그 방식은 영문 단복수
관례에 기대고 있어서 ``D_SA_ORG.ORG_CD`` 같은 웨어하우스 명명에서는 어간이
테이블 이름과 맞지 않아 실패한다.

여기서는 이름 대신 **기본 키** 를 쓴다. 차원 테이블의 기본 키 컬럼과 같은 이름의
컬럼이 다른 테이블에 있으면 그것을 참조로 본다. 기본 키는 선언된 사실이라
명명 관례보다 신뢰도가 높고, 복합 키도 그대로 다뤄진다.
"""

from __future__ import annotations

from tablefold.schema.ir import ForeignKey, PhysicalSchema, PhysicalTable

# 어느 테이블에나 있으면서 관계를 뜻하지 않는 적재 메타 컬럼.
_NON_KEY = frozenset({"load_dt", "load_user", "etl_id", "etl_dt"})

# 어느 테이블을 가리키는지 스스로 말해 주지 못하는 키 이름.
#
# 이런 이름을 참조 대상으로 허용하면 관계가 아니라 우연을 줍는다. ``id`` 를 기본
# 키로 쓰는 흔한 스키마에서는 모든 테이블이 서로의 ``id`` 를 갖고 있는 셈이 되어
# 완전 그래프가 만들어진다 — 3개 테이블에서 가짜 엣지 6개가 나온다.
#
# 웨어하우스 이름은 이 목록에 걸리지 않는다: ``ORG_CD``, ``ITEM_CD``, ``YYYYMMDD``
# 는 모두 무엇을 가리키는지 이름 안에 들고 있다.
_AMBIGUOUS_SINGLE_KEYS = frozenset(
    {"id", "cd", "code", "key", "no", "num", "seq", "name", "type", "status"}
)


def infer_from_primary_keys(
    schema: PhysicalSchema,
    *,
    targets: tuple[str, ...] | None = None,
    unique_subsets: dict[str, tuple[str, ...]] | None = None,
) -> tuple[ForeignKey, ...]:
    """기본 키를 참조 대상으로 삼아 관계를 복구한다.

    ``targets`` 는 참조 *대상* 이 될 수 있는 테이블을 제한한다. 스타 스키마에서는
    차원 테이블 목록을 주는 것이 맞다. 비워 두면 모든 테이블이 대상이 되는데,
    그러면 팩트끼리도 공유 키로 엮여서 실제로 존재하지 않는 관계가 대량으로
    생긴다 — 팩트는 서로를 참조하지 않는다.

    ``unique_subsets`` 는 기본 키의 *일부* 만으로도 행이 유일하게 결정되는 경우를
    알려 준다. 웨어하우스 차원은 기본 키에 계층 단계를 함께 넣는 일이 흔한데
    (``D_ITEM`` 의 ``(ITEM_GROUP_CD, ITEM_CD)``), 팩트는 말단 코드만 들고 있어서
    전체 키가 맞지 않는다. 그런 경우 실제 데이터로 유일성을 확인한 부분 키를
    넘기면 그것도 참조 대상이 된다. 유일성은 호출자가 확인해야 한다 — 이 함수는
    스키마만 보고 데이터를 읽지 않는다.

    한 테이블이 대상 키 컬럼을 *전부* 가지고 있을 때만 엣지를 만든다. 일부만
    겹치는 것은 우연이거나 다른 차원과의 관계다.
    """
    allowed = {t.lower() for t in targets} if targets is not None else None
    subsets = {k.lower(): v for k, v in (unique_subsets or {}).items()}

    referenced: list[PhysicalTable] = [
        t
        for t in schema.tables
        if t.primary_key and (allowed is None or t.name.lower() in allowed)
    ]

    declared = {
        (fk.from_table.lower(), tuple(c.lower() for c in fk.from_columns))
        for fk in schema.foreign_keys
    }

    found: list[ForeignKey] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()

    for source in schema.tables:
        available = {c.name.lower(): c.name for c in source.columns}
        for target in referenced:
            if target.name.lower() == source.name.lower():
                continue

            full = tuple(c for c in target.primary_key if c.lower() not in _NON_KEY)
            options = [full]
            if target.name.lower() in subsets:
                options.append(subsets[target.name.lower()])

            for key in options:
                if not key or not all(c.lower() in available for c in key):
                    continue
                if len(key) == 1 and key[0].lower() in _AMBIGUOUS_SINGLE_KEYS:
                    continue
                lowered = tuple(c.lower() for c in key)
                if (source.name.lower(), lowered) in declared:
                    continue
                mark = (source.name.lower(), lowered, target.name.lower())
                if mark in seen:
                    continue
                seen.add(mark)

                found.append(
                    ForeignKey(
                        from_table=source.name,
                        from_columns=tuple(available[c.lower()] for c in key),
                        to_table=target.name,
                        to_columns=tuple(key),
                        inferred=True,
                        confidence=0.9,
                    )
                )
    return tuple(found)
