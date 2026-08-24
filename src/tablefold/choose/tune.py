"""설정 하나 대신 **교환곡선**을 낸다.

노브를 튜닝하는 대신 프롬프트 길이를 쓸어 가며 "이만큼 주면 이만큼 답한다"를
잰다. 그러면 사용자가 고르는 것이 튜닝값이 아니라 **비용 대 커버리지**라는
비즈니스 결정이 된다.

포화점(:func:`knee`)이 특히 값싸다. 더 줘도 답이 안 늘어나는 지점이 있고, 그
위는 순수한 낭비다 — 실측에서 NL2SQL 픽스처가 596필드에서 포화했다. 그 값은
누가 정한 상수가 아니라 스키마와 질문이 정한다.

:mod:`tablefold.report.answerable` 이 LLM 을 부르지 않으므로 한 점당 비용이
폴드 한 번뿐이다. 수십 점을 쓸어도 몇 초다.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.ir import PhysicalSchema
from tablefold.report import answerable
from tablefold.report.prompt import render_text
from tablefold.t2sql.preset import STAR_MAX_HOPS, fold_star_schema

DEFAULT_BUDGETS = (4_000, 8_000, 12_000, 16_000, 20_000, 24_000, 32_000, 48_000)
"""쓸어 볼 프롬프트 길이(문자). 실측 레이어가 2만 자대라 그 주변을 촘촘히 둔다."""


@dataclass(frozen=True)
class CurvePoint:
    prompt_budget: int
    prompt_length: int
    models: int
    fields: int
    answered: int
    total: int
    answered_subjects: int
    total_subjects: int

    @property
    def rate(self) -> float:
        return self.answered / self.total if self.total else 1.0


def curve(
    schema: PhysicalSchema,
    cases,
    *,
    budgets: tuple[int, ...] = DEFAULT_BUDGETS,
    max_hops: int = STAR_MAX_HOPS,
) -> tuple[CurvePoint, ...]:
    """예산마다 접어 보고 골드셋으로 잰다.

    스키마는 **이미 준비된 것**을 받는다(관계 복구·기간 앵커). 여기서 다시
    준비하면 호출자가 쓰는 레이어와 다른 것을 재게 된다.
    """
    points: list[CurvePoint] = []
    for budget in budgets:
        result = fold_star_schema(schema, max_hops=max_hops, prompt_budget=budget)
        layer = result.layer
        report = answerable.measure(layer, cases, result.graph)
        points.append(
            CurvePoint(
                prompt_budget=budget,
                prompt_length=len(render_text(layer)),
                models=len(layer.models),
                fields=layer.field_count,
                answered=report.answered,
                total=report.total,
                answered_subjects=report.answered_subjects,
                total_subjects=len(report.subjects),
            )
        )
    return tuple(points)


def knee(points: tuple[CurvePoint, ...]) -> CurvePoint | None:
    """더 줘도 답이 안 늘어나는 **가장 작은** 예산.

    이 값이 추천이다. 위로는 낭비이고 아래로는 답을 잃는다. 누가 정한 상수가
    아니라 스키마와 질문이 정한다.
    """
    if not points:
        return None
    best = max(p.answered for p in points)
    return min((p for p in points if p.answered == best), key=lambda p: p.prompt_budget)


def render(points: tuple[CurvePoint, ...]) -> str:
    """사람이 읽을 곡선. 포화점에 표시를 단다."""
    if not points:
        return "no points measured"
    best = knee(points)
    lines = [
        f"{'예산(자)':>10}{'실제':>9}{'모델':>6}{'필드':>6}{'답변':>10}{'주제':>8}",
    ]
    for point in points:
        mark = "  ← 포화" if best is not None and point is best else ""
        lines.append(
            f"{point.prompt_budget:>10,}{point.prompt_length:>9,}"
            f"{point.models:>6}{point.fields:>6}"
            f"{point.answered:>6}/{point.total:<3}"
            f"{point.answered_subjects:>5}/{point.total_subjects:<3}{mark}"
        )
    if best is not None:
        lines.append("")
        lines.append(
            f"추천: prompt_budget={best.prompt_budget:,} — "
            f"{best.answered}/{best.total}건, 주제 "
            f"{best.answered_subjects}/{best.total_subjects}. "
            "더 올려도 답이 늘지 않는다."
        )
    return "\n".join(lines)
