"""질문에 답할 수 있는 레이어를 만든다. **CLI 와 화면이 같이 쓴다.**

이 모듈이 있는 이유는 갈라졌기 때문이다. 실측이 나온 설정(모든 팩트·차원 앵커,
관계 복구, 검증된 예시)이 한동안 ``demo/`` 안에만 있었고, CLI 는 기본 탐욕 폴드를
써서 **같은 스키마에 같은 질문을 해도 다른 답**이 나왔다. 웹 챗봇은 또 다른 세
번째 경로였다. 설정을 각자 들고 있으면 반드시 다시 갈라진다.

호출자가 하는 일은 스키마를 읽어 오는 것까지다. 그다음 "어떻게 접을 것인가"는
여기 한 벌만 있다.
"""

from __future__ import annotations

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

    @property
    def note(self) -> str:
        layer = self.result.layer
        how = "데이터로 검증" if self.validated else "스키마만 봄"
        return (
            f"{len(layer.models)}개 모델 · {layer.field_count}개 필드 · "
            f"외래 키 선언 {self.declared_keys} + 복구 {self.recovered_keys}"
            f"({how})"
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
    """
    declared = sum(1 for fk in schema.foreign_keys if not fk.inferred)
    recovered = already_recovered
    validated = already_recovered > 0 and cursor is None

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

    return Preparation(
        result=fold_star_schema(
            schema,
            max_hops=max_hops,
            prompt_budget=prompt_budget,
            infer_missing_keys=False,
        ),
        declared_keys=declared,
        recovered_keys=recovered,
        validated=validated,
    )
