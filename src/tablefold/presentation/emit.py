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

from tablefold.schema.ir import FieldKind, LogicalLayer, LogicalModel


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
            }
            for f in model.fields
        ],
    }


def render_text(layer: LogicalLayer) -> str:
    """Compact schema text, sized for a prompt.

    Fields are grouped by kind because the grouping carries a rule the reader
    needs: aggregated fields are already summarised over child rows, so
    wrapping one in another ``SUM`` double-counts.
    """
    header = (
        f"=== TIER-1 CORE WIDE MODELS ({len(layer.models)} models covering "
        f"{layer.covered_table_count}/{layer.source_table_count} physical tables) ==="
    )
    lines: list[str] = [header, ""]

    for model in layer.models:
        lines.append(f"### {model.name}")
        if model.description:
            lines.append(model.description)

        for kind, label in (
            (FieldKind.BASE, "Own columns"),
            (FieldKind.JOINED, "Joined in (one related row each)"),
            (FieldKind.AGGREGATED, "Aggregated from child rows"),
        ):
            group = [f for f in model.fields if f.source.kind is kind]
            if not group:
                continue
            lines.append(f"{label}:")
            for f in group:
                note = f" — {f.description}" if f.description else ""
                lines.append(f"  {f.name} ({f.type}){note}")
        lines.append("")

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
