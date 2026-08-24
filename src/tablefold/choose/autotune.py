"""폴드 설정을 탐색해 가장 좋은 것을 고른다.

**이 모듈의 핵심 규칙은 하나다: 모든 후보를 같은 자로 잰다.**

전에는 스타 프리셋과 그리드 후보의 점수 공식이 서로 달랐다. 스타는
``pair*60 + absorption*40`` 으로, 그리드는 커버리지·쌍·흡수·효율의 가중합으로
매겨졌고, 그 둘을 ``>`` 로 비교했다. 서로 다른 척도의 수를 비교한 것이라 어느
쪽이 이기는지는 스키마가 아니라 공식이 정했다. 게다가 스타의 점수는 아예
상수 98.5 였다 — 무엇을 접든 같은 점수가 나왔다.

두 번째 규칙: **추천한 설정으로 다시 접으면 잰 것과 같은 레이어가 나와야 한다.**
점수는 실제로 접어 본 레이어에서 나오고, 그 레이어를 만든 설정을 그대로 돌려준다.
화면이 스칼라 다섯 개로 스타 프리셋을 재현할 수 없으므로 ``anchor_mode`` 를 함께
싣는다 — 그것은 다른 셀렉터이지 같은 셀렉터의 다른 설정이 아니다.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from tablefold.choose.select import SelectionPolicy
from tablefold.fold import FoldResult, fold
from tablefold.ir import PhysicalSchema
from tablefold.relate.synthesize import add_period_anchor
from tablefold.report.fidelity import Fidelity, measure
from tablefold.report.prompt import render_text
from tablefold.t2sql.preset import (
    STAR_FIELD_BUDGET,
    STAR_MAX_HOPS,
    STAR_MAX_MODEL_FIELDS,
    fold_star_schema,
    recover_relationships,
)

# 실제로 접을 때 쓰는 옵션. 그리드 후보도 이 값으로 접는다.
#
# 전에는 그리드가 기본값(``expose_child_filters=False``,
# ``prefix_joined_fields=True``, ``max_model_fields=64``)으로 접었다. 화면은
# 스타/혼합 옵션으로 다시 그리므로 **점수를 낸 레이어와 그려진 레이어가 다른
# 것**이었다. 격자의 가격표가 실제로 지불한 가격이 아니었다는 뜻이다.
COMPOSE_DEFAULTS: dict[str, Any] = {
    "infer_missing_keys": False,
    "include_aggregates": True,
    "expose_child_filters": True,
    "prefix_joined_fields": False,
    "max_model_fields": STAR_MAX_MODEL_FIELDS,
}


@dataclass(frozen=True)
class AutoTuneResult:
    best_max_areas: int
    best_field_budget: int
    best_min_gain: float
    best_max_cost: float
    best_coverage: float
    score: float
    reason: str
    candidates_evaluated: int
    result: FoldResult

    best_prompt_budget: int = 0
    """이긴 레이어가 실제로 찍은 **문자 수**. 화면이 이 값을 넣으면 같은 레이어가
    나온다 — 필드 수로 재현하려 하면 이름·주석 길이 차이만큼 어긋난다."""

    anchor_mode: str = "auto"
    """이 결과를 재현하려면 화면이 어느 앵커 모드로 접어야 하는가.

    스칼라 다섯 개(``coverage`` · ``field_budget`` · ``min_gain`` · ``max_cost`` ·
    ``max_areas``)로는 스타 프리셋을 재현할 수 없다. 그것은 **다른 셀렉터**이지
    같은 셀렉터의 다른 설정이 아니다. 이 값이 없던 동안 화면은 스타 프리셋의
    점수를 띄운 뒤 탐욕 폴드를 그렸다 — 16모델·그룹 가능 100% 라고 보고하고
    8모델·73.7% 를 그렸다.
    """


# ── 점수 ──────────────────────────────────────────────────────────────────────

# 골드셋이 있으면 이 가중치는 **쓰지 않는다.** 답한 질문 수가 있는데 대리 지표를
# 가중합할 이유가 없다 — :mod:`tablefold.choose.tune` 을 쓴다. 아래는 골드셋이
# 없을 때의 폴백이고, 그 사실이 결과에 그대로 드러나야 한다.
WEIGHTS = {
    "coverage": 0.30,
    "pair": 0.25,
    "groupability": 0.20,
    "absorption": 0.15,
    "compactness": 0.10,
}
"""가중치를 한곳에 모은다. 흩어져 있으면 후보마다 다른 자를 쓰게 된다.

``groupability`` 를 넣은 이유는 흡수와 답변이 다르기 때문이다. 1:N 자식으로만
흡수된 표는 컬럼이 ``filter_only`` 라 ``GROUP BY`` 에 못 쓴다 — "거래처별" 질문이
답이 안 되는데 커버리지와 흡수율은 100% 를 보고한다.
"""

COMPACT_TARGET = 12_000
"""이 길이까지는 만점. 여기서부터 절반씩 깎인다 (문자 수).

전에는 ``100 - prompt_len/200`` 이었다. 2만 자짜리 웨어하우스 레이어에서는 값이
음수라 0 으로 잘렸고, 후보 사이에 아무 차이도 만들지 못하는 죽은 항이었다.
"""


def _compactness(prompt_length: int) -> float:
    """짧을수록 좋지만 **한없이** 좋지는 않다. 0~100.

    선형으로 재면 "테이블을 버려서 짧아진" 레이어가 이긴다. 반감기로 재면 목표
    안에서는 차이가 없고 넘어설 때만 완만하게 벌점이 붙는다.
    """
    if prompt_length <= COMPACT_TARGET:
        return 100.0
    return 100.0 * 0.5 ** ((prompt_length - COMPACT_TARGET) / COMPACT_TARGET)


def score_layer(result: FoldResult) -> tuple[float, Fidelity, int]:
    """``(점수 0~100, 반영도, 프롬프트 길이)``. **모든 후보가 이 함수를 쓴다.**

    **골드셋이 없을 때의 폴백이다.** 가중치와 :data:`COMPACT_TARGET` 은 정당화할
    수 없고, 그 값이 결론을 정한다 — 목표를 8천으로 바꾸면 "최적" 예산이 움직인다.
    질문 세트가 있으면 :func:`tablefold.choose.tune.curve` 가 답한 질문 수를 직접
    재므로 이 함수를 쓸 이유가 없다.
    """
    layer = result.layer
    report = measure(layer, result.graph)
    prompt_length = len(render_text(layer))

    coverage = layer.covered_table_count / max(1, layer.source_table_count)
    parts = {
        "coverage": coverage * 100.0,
        "pair": report.pair_answerability * 100.0,
        "groupability": report.table_groupability * 100.0,
        "absorption": report.join_absorption * 100.0,
        "compactness": _compactness(prompt_length),
    }
    total = sum(parts[k] * w for k, w in WEIGHTS.items())

    # 표를 버려서 점수를 사지 못하게 한다. 커버리지가 낮으면 나머지 축이 아무리
    # 좋아도 그건 "적게 담아서 잘 담았다"는 뜻이다.
    if coverage < 0.85:
        total *= coverage / 0.85

    return total, report, prompt_length


def _reason(label: str, result: FoldResult, report: Fidelity, length: int) -> str:
    layer = result.layer
    return (
        f"{label}: 커버리지 "
        f"{layer.covered_table_count / max(1, layer.source_table_count) * 100:.1f}%"
        f"({layer.covered_table_count}/{layer.source_table_count}개), "
        f"함께 읽기 {report.pair_answerability * 100:.1f}%, "
        f"그룹 가능 {report.table_groupability * 100:.1f}%, "
        f"조인 흡수율 {report.join_absorption * 100:.1f}% "
        f"(모델 {len(layer.models)}개, 프롬프트 {length:,}자)"
    )


# ── 후보 ──────────────────────────────────────────────────────────────────────

COVERAGE_OPTIONS = (0.85, 0.90, 0.95, 1.0)
BUDGET_OPTIONS = (150, 200, 300, 450)
MIN_GAIN_OPTIONS = (1.0, 2.0, 3.0)
MAX_COST_OPTIONS = (10.0, 15.0, 20.0)


@dataclass(frozen=True)
class _Candidate:
    label: str
    anchor_mode: str
    coverage: float
    field_budget: int
    min_gain: float
    max_cost: float


def _grid() -> tuple[_Candidate, ...]:
    return tuple(
        _Candidate(
            label=(
                f"coverage={cov}, budget={budget}, min_gain={gain}, max_cost={cost}"
            ),
            anchor_mode="auto",
            coverage=cov,
            field_budget=budget,
            min_gain=gain,
            max_cost=cost,
        )
        for cov in COVERAGE_OPTIONS
        for budget in BUDGET_OPTIONS
        for gain in MIN_GAIN_OPTIONS
        for cost in MAX_COST_OPTIONS
    )


def _sample(items: tuple[_Candidate, ...], limit: int) -> tuple[_Candidate, ...]:
    """격자를 ``limit`` 개로 **고르게** 줄인다.

    전에는 중첩 루프 안에서 ``break`` 로 잘랐다. 그러면 가장 바깥 축
    (``coverage``)이 첫 값에 고정된 채 안쪽 축만 돌아서, 격자의 4분의 1도 못
    보고 "24개를 평가했다"고 보고했다.
    """
    if limit <= 0:
        return ()
    if len(items) <= limit:
        return items
    stride = len(items) / limit
    return tuple(items[int(i * stride)] for i in range(limit))


# ── 탐색 ──────────────────────────────────────────────────────────────────────


def autotune_stream(
    schema: PhysicalSchema,
    *,
    max_hops: int = STAR_MAX_HOPS,
    max_candidates: int = 24,
    period_anchor: bool = True,
) -> Iterator[dict[str, Any]]:
    """후보마다 진행 이벤트를, 마지막에 ``done`` 을 낸다.

    ``prepare_for_questions`` 와 같은 준비를 거친다. 준비가 다르면 여기서 잰
    점수가 실제로 쓰이는 레이어의 점수가 아니게 된다.
    """
    schema = recover_relationships(schema)
    if period_anchor:
        schema = add_period_anchor(schema)

    grid = _sample(_grid(), max(0, max_candidates - 1))
    total = len(grid) + 1

    best_score = -1.0
    best_result: FoldResult | None = None
    best: _Candidate | None = None
    best_reason = ""
    evaluated = 0

    # 스타 프리셋도 **같은 자로** 잰다. 특별 취급하지 않는다.
    star = _Candidate(
        label="전체 팩트/차원 앵커링 (Star Preset)",
        anchor_mode="star",
        coverage=1.0,
        field_budget=STAR_FIELD_BUDGET,
        min_gain=1.0,
        max_cost=15.0,
    )

    for candidate in (star, *grid):
        evaluated += 1
        yield {
            "event": "progress",
            "current": evaluated,
            "total": total,
            "pct": round(evaluated / total * 100, 1),
            "evaluating": candidate.label,
            "current_best_score": round(max(best_score, 0.0), 1),
        }

        result = _fold_candidate(schema, candidate, max_hops=max_hops)
        if result is None:
            continue

        value, report, length = score_layer(result)
        if value > best_score:
            best_score = value
            best_result = result
            best = candidate
            best_reason = _reason(candidate.label, result, report, length)

    if best_result is None or best is None:
        fallback = fold(schema, max_hops=max_hops, **COMPOSE_DEFAULTS)
        value, report, length = score_layer(fallback)
        final = AutoTuneResult(
            best_max_areas=len(fallback.layer.models),
            best_field_budget=COMPOSE_DEFAULTS.get("field_budget", 200),
            best_min_gain=2.0,
            best_max_cost=10.0,
            best_coverage=0.90,
            score=round(value, 1),
            reason=_reason(
                "모든 후보가 실패해 기본 폴드로 물러섬", fallback, report, length
            ),
            candidates_evaluated=evaluated,
            result=fallback,
            best_prompt_budget=length,
            anchor_mode="auto",
        )
    else:
        final = AutoTuneResult(
            best_max_areas=len(best_result.layer.models),
            best_field_budget=best.field_budget,
            best_min_gain=best.min_gain,
            best_max_cost=best.max_cost,
            best_coverage=best.coverage,
            score=round(best_score, 1),
            reason=best_reason,
            candidates_evaluated=evaluated,
            result=best_result,
            best_prompt_budget=len(render_text(best_result.layer)),
            anchor_mode=best.anchor_mode,
        )

    yield {
        "event": "done",
        "result": {
            "max_areas": final.best_max_areas,
            "field_budget": final.best_field_budget,
            "prompt_budget": final.best_prompt_budget,
            "min_gain": final.best_min_gain,
            "max_cost": final.best_max_cost,
            "coverage": final.best_coverage,
            "score": final.score,
            "reason": final.reason,
            "candidates_evaluated": final.candidates_evaluated,
            "anchor_mode": final.anchor_mode,
        },
        "_result": final,
    }


def _fold_candidate(
    schema: PhysicalSchema, candidate: _Candidate, *, max_hops: int
) -> FoldResult | None:
    try:
        if candidate.anchor_mode == "star":
            return fold_star_schema(
                schema,
                max_hops=max_hops,
                field_budget=candidate.field_budget,
                infer_missing_keys=False,
            )
        return fold(
            schema,
            policy=SelectionPolicy(
                coverage_target=candidate.coverage,
                min_gain=candidate.min_gain,
                max_fields_per_table=candidate.max_cost,
            ),
            max_hops=max_hops,
            field_budget=candidate.field_budget,
            **COMPOSE_DEFAULTS,
        )
    except Exception:  # noqa: BLE001 — 나쁜 조합 하나가 탐색 전체를 죽이지 않는다
        return None


def autotune(
    schema: PhysicalSchema,
    *,
    max_hops: int = STAR_MAX_HOPS,
    max_candidates: int = 24,
    period_anchor: bool = True,
) -> AutoTuneResult:
    """가장 좋은 설정을 찾는다.

    :func:`autotune_stream` 을 끝까지 돌린 것과 **정확히 같다.** 두 벌로 두었을
    때 한쪽만 고쳐져서 화면과 CLI 의 추천이 갈라졌다.
    """
    final: AutoTuneResult | None = None
    for event in autotune_stream(
        schema,
        max_hops=max_hops,
        max_candidates=max_candidates,
        period_anchor=period_anchor,
    ):
        if event.get("event") == "done":
            final = event["_result"]
    assert final is not None  # 스트림은 항상 done 을 낸다
    return final
