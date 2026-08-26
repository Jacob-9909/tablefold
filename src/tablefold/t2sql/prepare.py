"""질문에 답할 수 있는 레이어를 만든다. **CLI 와 화면이 같이 쓴다.**

이 모듈이 있는 이유는 갈라졌기 때문이다. 실측이 나온 설정(모든 팩트·차원 앵커,
관계 복구, 검증된 예시)이 한동안 ``demo/`` 안에만 있었고, CLI 는 기본 탐욕 폴드를
써서 **같은 스키마에 같은 질문을 해도 다른 답**이 나왔다. 웹 챗봇은 또 다른 세
번째 경로였다. 설정을 각자 들고 있으면 반드시 다시 갈라진다.

호출자가 하는 일은 스키마를 읽어 오는 것까지다. 그다음 "어떻게 접을 것인가"는
여기 한 벌만 있다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tablefold.fold import FoldResult
from tablefold.ir import PhysicalSchema
from tablefold.read.mssql import DIALECT as LIVE_DIALECT
from tablefold.relate.synthesize import add_period_anchor
from tablefold.relate.validate import Cursor, recover_with_data
from tablefold.t2sql.preset import (
    STAR_MAX_HOPS,
    fold_star_schema,
    recover_relationships,
)

GENERIC_DIALECTS = frozenset({"", "postgres"})
"""아무도 고르지 않았다는 뜻의 값들.

``postgres`` 는 :func:`tablefold.rewrite.expand.expand` 의 기본값이라 "이 방언을
원한다"가 아니라 "안 정했다"는 뜻으로 흘러 들어온다. 실제로 정한 값은 덮지 않는다.
"""


def dialect_for_live(requested: str = "") -> str:
    """접속된 데이터베이스에 실제로 던질 수 있는 방언.

    라이브 소스는 MSSQL 이다. 기본값 ``postgres`` 를 그대로 두면 ``LIMIT`` 이
    섞여 나오고 실행 단계에서 죽는다 — 문법 오류라 조용하지도 않지만, 화면과
    CLI 가 서로 다른 SQL 을 내는 동안 CLI 만 맞는 것으로 보였다.
    """
    return LIVE_DIALECT if requested in GENERIC_DIALECTS else requested


@dataclass(frozen=True)
class Preparation:
    """접은 결과와, 어떻게 접었는지.

    ``validated`` 가 거짓이면 관계는 "스키마상 가능한" 것이지 관측된 것이 아니다.
    읽는 쪽이 그 차이를 알아야 한다 — 모델 개수가 다른 이유가 대개 이것이다.
    """

    result: FoldResult
    declared_keys: int
    recovered_keys: int
    validated: bool
    consolidated_tables: int = 0
    """기간 파티션(월별 스냅샷)을 한 벌로 합친 표 수.

    합쳐진 표는 원본 이름에서 접미사를 뗀 가상 테이블이 된다
    (:mod:`tablefold.relate.consolidate`). 0 이면 합쳐진 것이 없다는 뜻이다.
    """

    cardinality_measured: int = 0
    """데이터에서 읽은 (표, 컬럼) 카디널리티 수. 0 이면 측정이 없었다는 뜻이다.

    측정이 있었는지는 읽는 쪽이 알아야 한다 — 같은 예산이라도 컬럼을 고른 기준이
    "입력 순서"였는지 "정보량"이었는지는 신뢰가 다르다. 못 잰 것을 잰 것처럼
    보고하지 않기 위해 0 은 거짓이 아니라 "안 했다"로 읽는다.
    """

    @property
    def note(self) -> str:
        layer = self.result.layer
        how = "데이터로 검증" if self.validated else "스키마만 봄"
        merged = (
            f" · 파티션 결합 {self.consolidated_tables}"
            if self.consolidated_tables
            else ""
        )
        counted = (
            f" · 카디널리티 {self.cardinality_measured} 측정"
            if self.cardinality_measured
            else ""
        )
        return (
            f"{len(layer.models)}개 모델 · {layer.field_count}개 필드 · "
            f"외래 키 선언 {self.declared_keys} + 복구 {self.recovered_keys}"
            f"({how}){merged}{counted}"
        )


def prepare_for_questions(
    schema: PhysicalSchema,
    *,
    cursor: Cursor | None = None,
    recover: bool = True,
    already_recovered: int = 0,
    max_hops: int = STAR_MAX_HOPS,
    period_anchor: bool = True,
    prompt_budget: int | None = None,
    consolidate_partitions: bool = True,
    monthly_summaries: bool = False,
    dedupe_equivalents: bool = False,
    measure_cardinality: bool = False,
    expose_groupable_children: bool = False,
) -> Preparation:
    """질문에 답할 수 있게 접는다.

    ``cursor`` 를 주면 복구한 관계를 **데이터로 검증한다** — 참조 대상에 없는 값의
    비율을 세어 임계값을 넘는 후보를 버린다. 안 주면 스키마만 보고 "가능한" 관계를
    전부 만든다. 실측에서 이 차이가 모델 9개와 6개를 갈랐다.

    ``already_recovered`` 는 호출자가 이미 복구를 마치고 온 경우다(웹의 라이브
    소스가 그렇다). 두 번 복구하지 않으면서 표시는 정확하게 하려는 값이다.

    ``period_anchor`` 는 기간 차원이 없을 때 키에서 하나 세운다
    (:func:`~tablefold.relate.synthesize.add_period_anchor`). 실측 스키마에
    ``YYYYMM`` 이 7개 팩트에 있는데 캘린더 테이블이 없어서, 기간 입도에서 묻는
    질문을 받을 앵커가 아예 없었다. 이미 그 키를 소유한 표가 있으면 아무것도
    하지 않으므로 켜 두는 쪽이 기본이다.

    ``consolidate_partitions`` 는 월별 스냅샷(``F_LEDGER_202506`` …)을 가상
    테이블 한 벌로 합친다 (:func:`~tablefold.relate.consolidate.consolidate_snapshots`).
    합치지 않으면 같은 질문이 스냅샷 수만큼 모델로 갈라진다. 복구보다 먼저 하는
    이유는, 합친 뒤의 스키마가 더 작아서 관계 복구와 기간 앵커의 중복 일도
    줄어들기 때문이다.

    ``monthly_summaries`` 는 팩트마다 월 입도 요약 가상 테이블을 세운다
    (:func:`~tablefold.relate.synthesize.add_monthly_summaries`). 행이 진짜로
    줄어드는 유일한 압축이다 — 추세 질문은 요약, 상세 질문은 원본이 답한다.
    모델 수가 늘어나므로 기본은 꺼짐이다.

    ``dedupe_equivalents`` 는 값이 항상 함께 결정되는 컬럼 묶음을 찾아 레이어에서
    별칭을 접는다 (:mod:`tablefold.relate.equivalence`). 판정이 데이터라 커서가
    필요하고, 없으면 조용히 건너뛴다 — 이름으로 지어내지 않겠다는 약속이다.

    ``measure_cardinality`` 는 표당 한 번의 질의로 전체 컬럼의 distinct 를 일괄
    읽어(:func:`tablefold.report.information._cardinalities` 와 같은 방식) 예산
    경합에 정보량 비트를 얹는다. 접기 **전에** 읽는 이유는 이 값이 배분의 입력이
    되기 때문이다 — 접힌 뒤에 재봐야 이미 고른 뒤다. 커서가 없으면 켜 두어도
    아무것도 지어내지 않는다. 비공개 함수를 가져오는 것은
    :mod:`tablefold.report.answerable` 가 fidelity 의 ``_key_columns`` 를 쓰는
    것과 같은, 이 저장소의 통과 패턴이다.
    """
    declared = sum(1 for fk in schema.foreign_keys if not fk.inferred)
    recovered = already_recovered
    validated = already_recovered > 0 and cursor is None
    consolidated = 0
    snapshot_names: frozenset[str] = frozenset()

    if consolidate_partitions:
        from tablefold.relate.consolidate import consolidate_snapshots

        schema, reports = consolidate_snapshots(
            schema, cursor=cursor, dialect=dialect_for_live("")
        )
        consolidated = len(reports)
        # 스냅샷형(잔고 누적 위험)은 요약 합성에서 뺀다 — SUM 하면 잔고가
        # 기간 수만큼 더해진다. 판정 근거와 함께 경고로 남는다.
        snapshot_names = frozenset(r.virtual.name for r in reports if r.snapshot_like)

    if recover:
        if cursor is not None:
            schema, added = recover_with_data(schema, cursor)
            recovered += len(added)
            validated = True
        else:
            before = len(schema.foreign_keys)
            schema = recover_relationships(schema)
            recovered += len(schema.foreign_keys) - before

    if period_anchor:
        schema = add_period_anchor(schema)

    if monthly_summaries:
        from tablefold.relate.synthesize import add_monthly_summaries

        schema, _ = add_monthly_summaries(schema, exclude=snapshot_names)

    field_bits: dict[tuple[str, str], float] | None = None
    cardinality_measured = 0
    if measure_cardinality and cursor is not None:
        # 표당 **한 번의 질의**로 그 표 전체 컬럼의 distinct 를 읽는다. 컬럼마다
        # 묻으면 왕복이 컬럼 수만큼 늘고, 접은 뒤에 재면 이미 예산은 다 쓰인 뒤다.
        from tablefold.report.information import _cardinalities

        counts, _unmeasured = _cardinalities(schema, cursor, LIVE_DIALECT)
        cardinality_measured = len(counts)
        if counts:
            # 상수 컬럼도 설명에는 한 칸을 쓴다 — log2(1)=0 이면 공짜가 되는데,
            # 화면에서 지우는 것조차 공짜가 아니다(information.measure 와 같은 판단).
            field_bits = {pair: math.log2(max(d, 1)) + 1 for pair, d in counts.items()}

    result = fold_star_schema(
        schema,
        max_hops=max_hops,
        prompt_budget=prompt_budget,
        infer_missing_keys=False,
        field_bits=field_bits,
        expose_groupable_children=expose_groupable_children,
    )

    if dedupe_equivalents and cursor is not None:
        from dataclasses import replace as _dc_replace

        from tablefold.relate.equivalence import dedupe_fields, find_equivalents

        groups = find_equivalents(schema, cursor)
        layer, removed = dedupe_fields(result.layer, groups)
        if removed:
            aliases = tuple(
                (g.table.lower(), alias.lower())
                for g in groups
                for alias in g.columns[1:]
            )
            result = _dc_replace(result, layer=layer, merged_aliases=aliases)

    return Preparation(
        result=result,
        declared_keys=declared,
        recovered_keys=recovered,
        validated=validated,
        consolidated_tables=consolidated,
        cardinality_measured=cardinality_measured,
    )
