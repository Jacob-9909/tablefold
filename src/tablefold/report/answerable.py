"""골드셋을 목적함수로 쓴다.

노브(``field_budget`` · ``max_model_fields`` · ``coverage`` · ``min_gain`` ·
``max_cost``)가 존재하는 이유는 **목적함수가 없어서**다. 진짜 원하는 것은
"주어진 프롬프트 길이 안에서 가장 많은 업무 질문에 답하는 레이어"인데, 질문
분포를 모르니 대리 지표(커버리지·쌍·흡수)를 가중합하고 비용은 예산으로 막았다.
가중치는 정당화할 수 없고, 그 가중치가 결론을 정한다 —
:data:`tablefold.choose.autotune.COMPACT_TARGET` 을 8천으로 바꾸면 "최적" 예산이
움직인다.

골드셋이 있으면 그럴 필요가 없다. 정답 SQL 이 읽는 ``표.컬럼`` 을 레이어가 한
모델 안에서 전부 내놓을 수 있는지 세면 된다. **LLM 을 부르지 않는다** — 세는
것이므로 매번 같은 답이 나오고, 무료이고, 프롬프트 길이를 바꿔 가며 수백 번
돌릴 수 있다.

이건 "이 질문에 맞는 SQL 이 나온다"는 뜻이 아니다. **"답할 재료가 레이어에
있다"** 는 뜻이다. 재료가 없으면 어떤 LLM 도 못 답하므로 상한이고, 재료가 있어도
LLM 이 틀릴 수 있으므로 하한은 아니다. 압축 엔진의 책임은 정확히 이 상한이다.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.ir import LogicalLayer
from tablefold.relate.graph import SchemaGraph
from tablefold.report.fidelity import _key_columns


@dataclass(frozen=True)
class CaseVerdict:
    """골드셋 한 건에 대해 레이어가 재료를 갖췄는가."""

    case_id: str
    subject: str
    answerable: bool
    model: str | None
    """답할 수 있다면 어느 모델이. 여러 개면 필드가 가장 적은 쪽 — 읽는 쪽이
    실제로 고를 모델이다."""

    missing: tuple[str, ...]
    """모자란 ``표.컬럼``. 가장 아까운 모델 기준이라 "무엇을 더 담으면 되는가"가
    바로 읽힌다."""


@dataclass(frozen=True)
class Answerability:
    verdicts: tuple[CaseVerdict, ...]

    @property
    def answered(self) -> int:
        return sum(1 for v in self.verdicts if v.answerable)

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def rate(self) -> float:
        return self.answered / self.total if self.verdicts else 1.0

    @property
    def subjects(self) -> tuple[tuple[str, int, int], ...]:
        """``(주제, 답한 수, 전체)``.

        주제별로 보는 편이 낫다. 건수 비율은 질문이 몰린 주제에 끌려간다 —
        문서 6장이 "쌍 기준 100% ↔ 업무 주제 기준 5/9" 로 지적한 착시와 같은
        종류다.
        """
        buckets: dict[str, list[int]] = {}
        for verdict in self.verdicts:
            slot = buckets.setdefault(verdict.subject, [0, 0])
            slot[1] += 1
            if verdict.answerable:
                slot[0] += 1
        return tuple((k, v[0], v[1]) for k, v in sorted(buckets.items()))

    @property
    def answered_subjects(self) -> int:
        """한 건이라도 답할 수 있는 주제의 수."""
        return sum(1 for _, answered, _ in self.subjects if answered)


def _available(
    layer: LogicalLayer, graph: SchemaGraph
) -> dict[str, set[tuple[str, str]]]:
    """모델 이름 → 그 모델로 **닿을 수 있는** ``(표, 컬럼)`` (전부 소문자).

    두 가지를 합친다.

    * **필드로 나온 컬럼.** 필터 전용도 센다 — ``SELECT`` 은 못 해도 조건은 걸 수
      있으므로 답하는 능력의 관점에서는 살아 있다(``fidelity._exposed_columns``
      와 같은 규칙).
    * **흡수한 표의 키 컬럼.** 조인 키와 기본 키는 필드로 **일부러** 안 내놓는다 —
      참조 대상의 서술 컬럼이 그 자리에 승격되므로 원본 키는 의미 없는 코드값이
      된다. 그런데 정답 SQL 은 ``ON A.ORG_CD = B.ORG_CD`` 로 그 키를 쓴다. 필드로
      없다고 "답할 수 없다"고 세면, 실제로는 폴드가 자동으로 만들어 주는 조인을
      결손으로 세게 된다 — 실측에서 그 한 가지 때문에 50건 전부가 0 으로 나왔다.
    """
    keys = _key_columns(graph)
    found: dict[str, set[tuple[str, str]]] = {}
    for model in layer.models:
        available = {
            (f.source.table.lower(), f.source.column.lower()) for f in model.fields
        }
        for table in (model.base_table, *model.absorbed_tables):
            low = table.lower()
            available |= {(low, c) for c in keys.get(low, ())}
        found[model.name] = available
    return found


def measure(layer: LogicalLayer, cases, graph: SchemaGraph) -> Answerability:
    """골드셋 각 건에 대해 레이어가 재료를 갖췄는지 센다.

    *cases* 는 :class:`~tablefold.t2sql.goldset.GoldCase` 의 열이다. 이 모듈이
    그 타입을 import 하지 않는 이유는 순환이다 — ``t2sql`` 이 ``report`` 를 읽는다.
    """
    exposed = _available(layer, graph)
    verdicts: list[CaseVerdict] = []

    for case in cases:
        needed = {
            (table, column)
            for table, columns in case.gold_references.items()
            for column in columns
        }
        best_model: str | None = None
        best_missing = needed
        for name, available in exposed.items():
            missing = needed - available
            if (
                len(missing) < len(best_missing)
                or (
                    not missing
                    and best_model is not None
                    and len(exposed[best_model]) > len(available)
                )
                or not missing
                and best_model is None
            ):
                best_model, best_missing = name, missing

        verdicts.append(
            CaseVerdict(
                case_id=case.case_id,
                subject=case.subject,
                answerable=not best_missing and bool(needed),
                model=best_model if not best_missing else None,
                missing=tuple(sorted(f"{t}.{c}" for t, c in best_missing)),
            )
        )

    return Answerability(verdicts=tuple(verdicts))
