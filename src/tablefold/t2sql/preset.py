"""질문에 답할 수 있는 레이어를 만드는 폴드 설정.

기본 폴드(:func:`tablefold.fold.fold`)는 탐욕적 집합 커버로 앵커를 고른다. 스키마를
*덮는* 것이 목표이므로 모델 수가 적게 나오고, 그것이 기본값으로 맞다.

Text-to-SQL은 목표가 다르다. 덮는 것이 아니라 **묻는 것**이다. 4-4장의 대칭이
그대로 작동해야 한다 — 차원 앵커는 팩트끼리 잇고, 팩트 앵커는 차원끼리 잇는다.
둘 다 있어야 "매출 대 계획"과 "품목별 매출 상세"를 동시에 답할 수 있다. 그래서
여기서는 앵커를 고르지 않고 **전부 주고 중복만 뺀다**.

이 설정이 실측(NL2SQL 19테이블, 답변 가능 100%)이 나온 경로다. 그동안
``demo/app.py`` 안에만 있어서 라이브러리 사용자가 재현할 수 없었다.
"""

from __future__ import annotations

from tablefold.choose.select import ExplicitSelector, SelectionPolicy
from tablefold.fold import FoldResult, fold
from tablefold.ir import PhysicalSchema
from tablefold.relate.graph import SchemaGraph
from tablefold.relate.keys import infer_from_primary_keys

# 별칭 없는 넓은 모델은 필드가 많다. 기본 예산(200 / 모델당 64)으로는 12개 표를
# 안는 모델에서 팩트 하나가 통째로 잘려 나가고, 그 주제의 질문이 답이 안 된다.
#
# 400 → 450 으로 올렸다. 레이어가 담아야 할 것이 실제로 늘었기 때문이다 —
# 합성된 기간 앵커(:mod:`tablefold.relate.synthesize`)가 모델 하나를 더하고,
# 이름 충돌로 조용히 버려지던 필드가 이제 한정 이름으로 살아남는다. 400 에서는
# ``D_FI_ORG`` 의 ``f_pls_PL_ACCT2_NM`` 통로가 잘려 "매출액 계정의 손익금액"
# 예시가 죽었다. 실측 곡선: 400→3/4, 450→4/4, 596 에서 포화(더 올려도 안 늘어난다).
STAR_FIELD_BUDGET = 450
STAR_MAX_MODEL_FIELDS = 200

# 정방향 2홉이면 팩트 → 차원 → 상위 차원까지 닿는다. 3홉은 웨어하우스에서
# 차원끼리 얽힌 경로를 주워 모델만 부풀린다.
STAR_MAX_HOPS = 2


def split_anchors(graph: SchemaGraph) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(참조되는 표, 참조만 하는 표)`` — 차원 후보와 팩트 후보.

    이름 규칙(``D_`` / ``F_`` 접두사)을 쓰지 않는다. 그 규칙은 이 웨어하우스
    하나의 관례이고, 다른 스키마에서는 아무 뜻이 없다.

    :mod:`tablefold.choose.classify` 의 점수도 쓰지 않는다. 점수는 컬럼의 성질을
    보는데, 측정값이 적은 팩트는 임계값 아래로 떨어진다 — 이 픽스처에서 ``F_PL``
    (0.333) · ``F_BS`` (0.324) · ``F_HR_PAY`` (0.226) 셋이 모두 차원으로 분류된다.

    대신 **구조**를 본다. 스타 스키마에서 팩트는 차원을 참조하고 아무도 팩트를
    참조하지 않는다. 참조당하는 쪽이 차원이고, 참조만 하는 쪽이 팩트다. 이건
    그래프에서 읽히는 사실이라 명명 규칙에도 컬럼 통계에도 기대지 않는다.

    양쪽에 다 걸리는 표(스노우플레이크의 중간 차원)는 차원으로 센다. 참조당하는
    표는 다른 표들을 한 모델에 모으는 자리이고, 그것이 앵커로서의 값어치다.
    """
    dimensions: list[str] = []
    facts: list[str] = []
    for table in graph.schema.tables:
        if graph.in_degree(table.name):
            dimensions.append(table.name)
        elif graph.out_degree(table.name):
            facts.append(table.name)
    return tuple(dimensions), tuple(facts)


def recover_relationships(schema: PhysicalSchema) -> PhysicalSchema:
    """차원의 기본 키로 관계를 복구해 붙인 스키마.

    웨어하우스는 외래 키를 잘 선언하지 않는다. 선언된 것만 쓰면 그래프가 조각나고,
    조각난 그래프에서는 팩트와 차원이 한 모델에서 만나지 못한다. 골드셋에서
    ``F_STOCK`` 과 ``D_SA_ORG`` 가 같은 질문에 나오는데 둘 사이에 선언된 키가
    없는 식이다.

    참조 *대상* 은 **나가는 키가 없는 표** 로 제한한다. 제한하지 않으면 팩트끼리도
    공유 키(``ORG_CD``)로 엮여서 실제로 없는 관계가 대량으로 생긴다 — 팩트는 서로를
    참조하지 않는다.

    :func:`split_anchors` 를 쓰지 않는 이유는 순환이다. 저쪽은 "참조당하는가"를
    보는데, 복구 전에는 아무도 참조하지 않는 차원이 있을 수 있다 — 그게 애초에
    복구하려는 상황이다. 그런 표는 ``in_degree`` 도 ``out_degree`` 도 0 이라
    차원으로도 팩트로도 안 잡히고 조용히 대상에서 빠진다. "나가는 키가 없다"는
    엣지가 하나도 없어도 읽히는 사실이다.

    **데이터를 읽지 않는다.** 그래서 여기서 나온 엣지는 "스키마상 가능한 관계"이지
    관측된 관계가 아니고, ``confidence`` 는 자리표시자 0.9 로 남는다. 데이터가 있으면
    후보마다 위반율을 재서 걸러야 한다 — ``demo/live.py`` 가 그렇게 한다. 그
    한 단계가 빠진 결과라는 것을 호출자가 알아야 하므로 이 함수는 별도로 둔다.
    ``fold_star_schema`` 가 자동으로 부르지 않는 이유가 그것이다.
    """
    graph = SchemaGraph.build(schema)
    targets = tuple(
        table.name
        for table in schema.tables
        if not graph.out_degree(table.name) and table.primary_key
    )
    if not targets:
        return schema
    recovered = infer_from_primary_keys(schema, targets=targets)
    if not recovered:
        return schema
    return schema.with_foreign_keys(schema.foreign_keys + recovered)


def fold_star_schema(
    schema: PhysicalSchema,
    *,
    infer_missing_keys: bool = False,
    max_hops: int = STAR_MAX_HOPS,
    field_budget: int = STAR_FIELD_BUDGET,
    prompt_budget: int | None = None,
    max_model_fields: int = STAR_MAX_MODEL_FIELDS,
) -> FoldResult:
    """팩트와 차원을 모두 앵커로 두고, 아무것도 새로 사지 않는 앵커만 뺀다.

    ``infer_missing_keys`` 기본값이 꺼짐인 이유는, 관계가 이미 있는 스키마에
    이름 기반 추론을 얹으면 없는 엣지가 생겨서 모델이 부푸는 쪽이 더 흔하기
    때문이다. 외래 키가 선언되지 않은 스키마라면 켜거나, 데이터로 검증한 키를
    미리 붙여서 넘긴다(:mod:`tablefold.relate.keys` 참고).

    앵커가 하나도 안 잡히면(관계가 없는 스키마) 기본 탐욕 폴드로 물러선다.
    빈 레이어를 돌려주면 질문이 전부 실패하는데, 그건 스키마 탓이지 설정 탓이
    아니라는 걸 호출자가 알 방법이 없다.
    """
    graph = SchemaGraph.build(schema)
    dimensions, facts = split_anchors(graph)
    anchors = facts + dimensions

    if not anchors:
        return fold(
            schema,
            max_hops=max_hops,
            field_budget=field_budget,
            prompt_budget=prompt_budget,
            max_model_fields=max_model_fields,
            infer_missing_keys=infer_missing_keys,
        )

    return fold(
        schema,
        selector=ExplicitSelector(anchors, prune_redundant=True),
        # 앵커를 이름으로 지목했으면 개수 상한은 그 개수다. 낮은 상한이 조용히
        # 뒤쪽 앵커를 자르면 답변 가능 범위가 무너지는데 레이어에는 아무 흔적도
        # 남지 않는다.
        policy=SelectionPolicy(max_areas=len(anchors)),
        max_hops=max_hops,
        field_budget=field_budget,
        prompt_budget=prompt_budget,
        max_model_fields=max_model_fields,
        infer_missing_keys=infer_missing_keys,
        include_aggregates=True,
        # 사전집계된 값에 기간·계정 조건을 걸 통로. 이게 없으면 "이번 달 매출"을
        # 물을 수단이 없다.
        expose_child_filters=True,
        # 웨어하우스 표 이름은 사람이 읽을 말이 아니다. ``D_SA_ORG.HEAD_NM`` 이
        # ``d_sa_org_HEAD_NM`` 이 되면 실제 질의가 쓰는 이름과 어긋난다.
        prefix_joined_fields=False,
    )
