"""논리 레이어를 직렬화(Serialise)하고 다양한 형식으로 출력합니다.

3가지 대상별 렌더링 형식:

* :func:`to_dict` — 각 필드의 출처(provenance) 정보가 포함된 전체 레이어 구조.
* :func:`render_text` — LLM 프롬프트 입력용 콤팩트 텍스트 포맷.
* :func:`render_report` — 실행 후 폴딩 결과를 평가/보고하기 위한 리포트.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from tablefold.ir import FieldKind, LogicalLayer, LogicalModel


def to_dict(layer: LogicalLayer) -> dict[str, Any]:
    return {
        "version": 1,
        "source_table_count": layer.source_table_count,
        "covered_table_count": layer.covered_table_count,
        "coverage": round(layer.coverage, 4),
        "stop_reason": layer.stop_reason,
        "selector": layer.selector,
        "model_count": len(layer.models),
        "compression_ratio": round(layer.compression_ratio, 2),
        "models": [_model_to_dict(m) for m in layer.models],
        "notes": list(layer.notes),
    }


def to_yaml(layer: LogicalLayer) -> str:
    return yaml.safe_dump(to_dict(layer), sort_keys=False, allow_unicode=True)


def to_json(layer: LogicalLayer) -> str:
    return json.dumps(to_dict(layer), indent=2, ensure_ascii=False)


def _model_to_dict(model: LogicalModel) -> dict[str, Any]:
    return {
        "name": model.name,
        "base_table": model.base_table,
        "description": model.description,
        "absorbed_tables": list(model.absorbed_tables),
        "fields": [
            {
                "name": f.name,
                "type": f.type,
                "kind": f.source.kind.value,
                "source": {
                    "table": f.source.table,
                    "column": f.source.column,
                    "aggregate": f.source.aggregate,
                    "path": [
                        {
                            "from": f"{s.from_table}({', '.join(s.from_columns)})",
                            "to": f"{s.to_table}({', '.join(s.to_columns)})",
                            "cardinality": s.cardinality.value,
                        }
                        for s in f.source.path
                    ],
                },
                "description": f.description,
                "filter_only": f.filter_only,
            }
            for f in model.fields
        ],
    }


def render_model(model: LogicalModel) -> str:
    """모델 하나의 정의. ``render_text`` 가 이걸 모아 레이어 전체를 만든다.

    낱개로 뽑아 쓸 수 있어야 하는 이유는 프롬프트가 모델 하나만 담을 수 있기
    때문이다(:mod:`tablefold.t2sql`). 그리고 **바이트가 안정적이어야** 한다 —
    같은 모델에 대해 매번 같은 문자열이 나와야 프롬프트 캐시가 붙는다. 여기에
    타임스탬프나 질문에 따라 달라지는 것을 섞으면 캐시가 조용히 죽는다.
    """
    lines = [f"### {model.name}"]
    if model.description:
        lines.append(model.description)

    groups = (
        (
            "Own columns",
            [
                f
                for f in model.fields
                if f.source.kind is FieldKind.BASE and not f.filter_only
            ],
        ),
        (
            "Joined in (one related row each)",
            [
                f
                for f in model.fields
                if f.source.kind is FieldKind.JOINED and not f.filter_only
            ],
        ),
        (
            "Aggregated from child rows — 자식 행에 대해 이미 합산된 총계. "
            "여러 행을 묶으려면 SUM 을 다시 써도 된다(이중 계산 아님). "
            "AVG 는 총계들의 평균이 되어 뜻이 달라진다",
            [
                f
                for f in model.fields
                if f.source.kind is FieldKind.AGGREGATED and not f.filter_only
            ],
        ),
        (
            "WHERE 전용 — 위 집계에 조건을 걸 때만 쓴다. "
            "SELECT 나 GROUP BY 에 쓸 수 없다",
            [f for f in model.fields if f.filter_only],
        ),
    )

    for label, group in groups:
        if not group:
            continue
        lines.append(f"{label}:")
        lines.extend(
            f"  {f.name} ({f.type})" + (f" — {f.description}" if f.description else "")
            for f in group
        )
    lines.append("")
    return "\n".join(lines)


def render_text(layer: LogicalLayer) -> str:
    """Compact schema text, sized for a prompt.

    필드를 종류별로 묶는 이유는 분류가 예뻐서가 아니라, 묶음마다 읽는 쪽이 어겨서는
    안 되는 규칙이 하나씩 달려 있기 때문이다. 그 규칙을 암시로만 두면 지켜지지 않는다.
    그래서 각 묶음의 계약을 문장으로 적는다:

    * 집계 필드는 자식 행에 대해 **이미 한 번** 합산된 값이다. 앵커 한 행에서 그
      값은 개별 행이 아니라 총계다. 여러 앵커 행에 걸쳐 ``SUM`` 으로 다시 묶는 것은
      정상이며 이중 계산이 아니다 — 자식 행 하나는 앵커 행 하나에만 속하기 때문이다.
      (NL2SQL 실측: ``SUM(f_sales_SALES_AMT_sum)`` 이 ``SUM(F_SALES.SALES_AMT)`` 와
      마지막 자리까지 일치했다.) 뜻이 달라지는 것은 ``AVG`` 쪽이다. 총계들의 평균은
      개별 행의 평균이 아니다.
    * 필터 전용 필드는 그 집계 안으로 밀어 넣을 조건을 받는 자리다. ``SELECT`` 에
      쓰면 앵커 한 행에 대응하는 값이 없어 뜻이 성립하지 않는다.
    * 모델은 하나만 읽는다. 두 모델을 ``JOIN`` 하는 순간 이 레이어가 없애려던 문제가
      그대로 돌아온다.
    """
    header = (
        f"=== TIER-1 CORE WIDE MODELS ({len(layer.models)} models covering "
        f"{layer.covered_table_count}/{layer.source_table_count} physical tables) ==="
    )
    lines: list[str] = [
        header,
        "각 모델은 넓은 표 하나다. 한 번에 한 모델만 조회하고, "
        "모델끼리 JOIN 하지 않는다.",
        "",
    ]

    for model in layer.models:
        lines.append(render_model(model))

    if layer.notes:
        lines.append("=== TIER-2 EDGE TABLES (On-Demand / Specific Query Fallback) ===")
        lines.extend(f"  {note.removeprefix('uncovered: ')}" for note in layer.notes)
        lines.append("")

    return "\n".join(lines)


# Why selection stopped, in words, with the knob that would change it. The
# model count is chosen by the objective now, so a reader who expected a
# different number needs to know which lever moved it.
_STOP_EXPLANATIONS = {
    "coverage_reached": "coverage target met",
    "gain_exhausted": "nothing left clears the admission rules — raise "
    "--max-cost, lower --min-gain, or raise --max-hops for more coverage",
    "max_areas": "hit the --max-models ceiling before the coverage target",
    "no_candidates": "schema has no tables",
    "selector_chose": "the selector returned this set explicitly",
}


def render_report(layer: LogicalLayer) -> str:
    """Per-model summary for a human reviewing the fold."""
    ratio = layer.compression_ratio
    lines = [
        f"{layer.source_table_count} tables -> {len(layer.models)} models "
        f"({ratio:.1f}:1), "
        f"{layer.covered_table_count} covered ({layer.coverage * 100:.0f}%)",
    ]
    if layer.selector and layer.selector != "greedy":
        lines.append(f"anchors chosen by: {layer.selector}")
    if layer.stop_reason:
        explanation = _STOP_EXPLANATIONS.get(layer.stop_reason, layer.stop_reason)
        lines.append(f"stopped: {explanation}")
    lines.append("")
    for model in layer.models:
        counts = {kind: 0 for kind in FieldKind}
        for f in model.fields:
            counts[f.source.kind] += 1
        lines.append(
            f"  {model.name:<24} {len(model.fields):>3} fields "
            f"(base {counts[FieldKind.BASE]}, "
            f"joined {counts[FieldKind.JOINED]}, "
            f"agg {counts[FieldKind.AGGREGATED]})  "
            f"absorbs {len(model.absorbed_tables) + 1} tables"
        )
    if layer.notes:
        lines.append("")
        lines.append(f"  {len(layer.notes)} tables uncovered")
    return "\n".join(lines)
