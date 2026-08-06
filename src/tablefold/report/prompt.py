"""논리 레이어를 직렬화(Serialise)하고 다양한 형식으로 출력합니다.

3가지 대상별 렌더링 형식:

* :func:`to_dict` — 각 필드의 출처(provenance) 정보가 포함된 전체 레이어 구조.
* :func:`render_text` — LLM 프롬프트 입력용 콤팩트 텍스트 포맷.
* :func:`render_report` — 실행 후 폴딩 결과를 평가/보고하기 위한 리포트.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from tablefold.ir import (
    Cardinality,
    FieldKind,
    FieldSource,
    JoinStep,
    LogicalField,
    LogicalLayer,
    LogicalModel,
)


def to_dict(layer: LogicalLayer) -> dict[str, Any]:
    return {
        "version": 1,
        "source_table_count": layer.source_table_count,
        "source_column_count": layer.source_column_count,
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
                            # 파생키·비등가는 ``from``/``to`` 문자열에 안 담긴다.
                            # 빠뜨리면 되읽은 레이어가 등가 조인을 만들어, 값이
                            # 조용히 틀린 SQL 이 나온다.
                            "key_expressions": (
                                list(s.key_expressions) if s.key_expressions else None
                            ),
                            "condition": s.condition,
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


GROUP_LABELS = (
    "Own columns",
    "Joined in (one related row each)",
    "Aggregated from child rows — 자식 행에 대해 이미 합산된 총계. "
    "여러 행을 묶으려면 SUM 을 다시 써도 된다(이중 계산 아님). "
    "AVG 는 총계들의 평균이 되어 뜻이 달라진다",
    "WHERE 전용 — 위 집계에 조건을 걸 때만 쓴다. "
    "SELECT 나 GROUP BY 에 쓸 수 없다",
)
"""필드 묶음의 제목. :func:`render_model` 과 :func:`model_overhead` 가 함께 읽는다.

한 벌만 두는 이유는 ``cost.py`` 와 같다 — 예산을 세는 쪽과 실제로 찍는 쪽이 다른
문자열을 읽으면 추정과 실제가 조용히 어긋난다.
"""


def field_line(field: LogicalField) -> str:
    """``render_model`` 이 필드 하나에 찍는 줄. 예산의 단위다."""
    suffix = f" — {field.description}" if field.description else ""
    return f"  {field.name} ({field.type}){suffix}"


def field_cost(field: LogicalField) -> int:
    """필드 하나가 프롬프트에서 차지하는 문자 수 (줄바꿈 포함)."""
    return len(field_line(field)) + 1


def model_overhead(name: str, description: str | None) -> int:
    """필드가 하나도 없어도 드는 문자 수 — 제목·설명·묶음 제목.

    묶음 제목은 비어 있으면 안 찍히지만, 넉넉히 잡는 편이 낫다. 모자라게 잡으면
    예산을 넘겨서 프롬프트가 잘린다.
    """
    total = len(f"### {name}") + 1
    if description:
        total += len(description) + 1
    total += sum(len(label) + 2 for label in GROUP_LABELS)
    return total + 1


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
            GROUP_LABELS[0],
            [
                f
                for f in model.fields
                if f.source.kind is FieldKind.BASE and not f.filter_only
            ],
        ),
        (
            GROUP_LABELS[1],
            [
                f
                for f in model.fields
                if f.source.kind is FieldKind.JOINED and not f.filter_only
            ],
        ),
        (
            GROUP_LABELS[2],
            [
                f
                for f in model.fields
                if f.source.kind is FieldKind.AGGREGATED and not f.filter_only
            ],
        ),
        (
            GROUP_LABELS[3],
            [f for f in model.fields if f.filter_only],
        ),
    )

    for label, group in groups:
        if not group:
            continue
        lines.append(f"{label}:")
        lines.extend(field_line(f) for f in group)
    lines.append("")
    return "\n".join(lines)


def layer_overhead(
    model_count: int, covered: int, total: int, notes: tuple[str, ...]
) -> int:
    """모델 필드를 빼고 :func:`render_text` 가 찍는 문자 수.

    머리말과 Tier-2 목록도 프롬프트다. 예산에서 먼저 빼지 않으면 편입은 예산을
    지켰는데 렌더된 결과가 넘친다 — retail 에서 6,000자 예산에 6,163자가 나왔다.
    """
    total_chars = len(_header(model_count, covered, total)) + 1
    total_chars += len(_INTRO) + 2
    if notes:
        total_chars += len(_TIER2_HEADER) + 1
        total_chars += sum(len(n.removeprefix("uncovered: ")) + 3 for n in notes)
        total_chars += 1
    return total_chars


def _header(model_count: int, covered: int, total: int) -> str:
    return (
        f"=== TIER-1 CORE WIDE MODELS ({model_count} models covering "
        f"{covered}/{total} physical tables) ==="
    )


_INTRO = (
    "각 모델은 넓은 표 하나다. 한 번에 한 모델만 조회하고, "
    "모델끼리 JOIN 하지 않는다."
)
_TIER2_HEADER = "=== TIER-2 EDGE TABLES (On-Demand / Specific Query Fallback) ==="


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
    lines: list[str] = [
        _header(
            len(layer.models), layer.covered_table_count, layer.source_table_count
        ),
        _INTRO,
        "",
    ]

    for model in layer.models:
        lines.append(render_model(model))

    if layer.notes:
        lines.append(_TIER2_HEADER)
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


# ── 되읽기 ────────────────────────────────────────────────────────────────────
#
# ``expand --layer`` 가 승인된 레이어를 재사용하려면 :func:`to_dict` 의 역이
# 필요하다. 없는 동안 CLI 는 ``--layer`` 를 받아 놓고 경고만 찍은 뒤 스키마에서
# 다시 접었다 — 승인한 레이어와 다른 레이어를 상대로 확장할 수 있었다는 뜻이다.


def from_dict(data: dict[str, Any]) -> LogicalLayer:
    """:func:`to_dict` 가 쓴 것을 되읽는다."""
    version = data.get("version")
    if version != 1:
        raise ValueError(f"unsupported layer version: {version!r}")
    return LogicalLayer(
        models=tuple(_model_from_dict(m) for m in data.get("models", ())),
        source_table_count=int(data.get("source_table_count", 0)),
        source_column_count=int(data.get("source_column_count", 0)),
        covered_table_count=int(data.get("covered_table_count", 0)),
        stop_reason=data.get("stop_reason"),
        selector=data.get("selector"),
        notes=tuple(data.get("notes", ())),
    )


def from_json(text: str) -> LogicalLayer:
    return from_dict(json.loads(text))


def from_yaml(text: str) -> LogicalLayer:
    return from_dict(yaml.safe_load(text))


def load(text: str) -> LogicalLayer:
    """JSON 이든 YAML 이든 받는다. ``fold -f`` 가 둘 다 쓸 수 있기 때문이다."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return from_json(text)
    return from_yaml(text)


_ENDPOINT = re.compile(r"^\s*(?P<table>[^(]+?)\s*\(\s*(?P<columns>.*?)\s*\)\s*$")


def _endpoint(text: str) -> tuple[str, tuple[str, ...]]:
    """``"orders(buyer_id, seller_id)"`` → ``("orders", ("buyer_id", "seller_id"))``."""
    found = _ENDPOINT.match(text)
    if found is None:
        raise ValueError(f"malformed join endpoint: {text!r}")
    columns = tuple(
        c.strip() for c in found.group("columns").split(",") if c.strip()
    )
    return found.group("table"), columns


def _step_from_dict(data: dict[str, Any]) -> JoinStep:
    from_table, from_columns = _endpoint(data["from"])
    to_table, to_columns = _endpoint(data["to"])
    expressions = data.get("key_expressions")
    return JoinStep(
        from_table=from_table,
        from_columns=from_columns,
        to_table=to_table,
        to_columns=to_columns,
        cardinality=Cardinality(data["cardinality"]),
        key_expressions=tuple(expressions) if expressions else None,
        condition=data.get("condition"),
    )


def _model_from_dict(data: dict[str, Any]) -> LogicalModel:
    return LogicalModel(
        name=data["name"],
        base_table=data["base_table"],
        description=data.get("description"),
        absorbed_tables=tuple(data.get("absorbed_tables", ())),
        fields=tuple(
            LogicalField(
                name=f["name"],
                type=f["type"],
                source=FieldSource(
                    kind=FieldKind(f["kind"]),
                    table=f["source"]["table"],
                    column=f["source"]["column"],
                    aggregate=f["source"].get("aggregate"),
                    path=tuple(
                        _step_from_dict(s) for s in f["source"].get("path", ())
                    ),
                ),
                description=f.get("description"),
                filter_only=bool(f.get("filter_only", False)),
            )
            for f in data.get("fields", ())
        ),
    )
