"""레이어를 그림으로 그릴 수 있는 그래프 형태로 내보낸다.

:mod:`emit` 의 ``to_dict`` 는 모델 → 필드 → 출처 순의 트리다. 읽는 쪽이 사람이 아니라
파서일 때는 그게 맞지만, 화면에 ERD 처럼 그리려면 반대 방향의 색인이 필요하다:
*이 물리 테이블은 어느 모델에 어떻게 들어갔고, 컬럼 몇 개를 보탰는가.*

그래서 이 모듈은 같은 사실을 노드와 엣지로 뒤집어 놓는다. 새 정보를 만들지 않는다 —
:mod:`tablefold.composition.compose` 가 필드마다 이미 기록해 둔 경로를 모아 셀 뿐이다.
"""

from __future__ import annotations

from typing import Any

from tablefold.graph.graph import SchemaGraph
from tablefold.schema.ir import Cardinality, FieldKind, LogicalLayer, LogicalModel

_ROLE_OF_KIND = {
    FieldKind.BASE: "anchor",
    FieldKind.JOINED: "inlined",
    FieldKind.AGGREGATED: "aggregated",
}


def to_graph(
    layer: LogicalLayer,
    graph: SchemaGraph,
    *,
    profiles: dict[str, Any] | None = None,
    criteria: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """화면이 그대로 그릴 수 있는 ``{nodes, edges, models, criteria}``.

    *profiles* 는 테이블 이름(소문자) → ``{"score", "role", ...}``. 있으면 노드에
    붙는다. 없으면 노드는 스키마에서 읽을 수 있는 것만 들고 나간다.
    """
    scores = profiles or {}
    covered = {
        t for m in layer.models for t in ({m.base_table.lower()} | _absorbed(m))
    }

    nodes = [
        {
            "id": t.name.lower(),
            "label": t.name,
            "kind": "table",
            "columns": len(t.columns),
            "primary_key": list(t.primary_key),
            "rows": t.row_estimate,
            "covered": t.name.lower() in covered,
            **_profile_of(scores, t.name),
        }
        for t in graph.schema.tables
    ]

    edges: list[dict[str, Any]] = [
        {
            "source": fk.from_table.lower(),
            "target": fk.to_table.lower(),
            "kind": "fk",
            "columns": list(fk.from_columns),
            "to_columns": list(fk.to_columns),
            "inferred": fk.inferred,
            "confidence": round(fk.confidence, 3),
        }
        for fk in graph.schema.foreign_keys
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "models": [_model_lineage(m) for m in layer.models],
        "criteria": {
            "selector": layer.selector,
            "stop_reason": layer.stop_reason,
            "field_count": layer.field_count,
            "source_table_count": layer.source_table_count,
            "covered_table_count": layer.covered_table_count,
            **(criteria or {}),
        },
    }


def _model_lineage(model: LogicalModel) -> dict[str, Any]:
    """모델 하나가 어떤 테이블에서 무엇을 가져왔는지, 테이블 단위로 되짚는다."""
    by_table: dict[str, dict[str, Any]] = {}

    for f in model.fields:
        low = f.source.table.lower()
        entry = by_table.setdefault(
            low,
            {
                "table": f.source.table,
                "role": _ROLE_OF_KIND[f.source.kind],
                "hops": f.source.hops,
                "fields": [],
                "join_columns": [],
                "cardinality": "base",
            },
        )
        entry["fields"].append(
            {
                "name": f.name,
                "column": f.source.column,
                "type": f.type,
                "aggregate": f.source.aggregate,
                "filter_only": f.filter_only,
            }
        )
        if f.source.path:
            last = f.source.path[-1]
            entry["join_columns"] = [
                f"{last.from_table}.{c}" for c in last.from_columns
            ] + [f"{last.to_table}.{c}" for c in last.to_columns]
            entry["cardinality"] = (
                "one_to_many"
                if any(s.cardinality is Cardinality.ONE_TO_MANY for s in f.source.path)
                else "many_to_one"
            )
            entry["path"] = [
                f"{s.from_table} → {s.to_table}" for s in f.source.path
            ]

    sources = sorted(
        by_table.values(),
        key=lambda e: (e["hops"], -len(e["fields"]), e["table"]),
    )
    for entry in sources:
        entry["field_count"] = len(entry["fields"])

    return {
        "name": model.name,
        "anchor": model.base_table,
        "description": model.description,
        "field_count": len(model.fields),
        "table_count": len(model.absorbed_tables) + 1,
        "sources": sources,
    }


def _absorbed(model: LogicalModel) -> set[str]:
    return {t.lower() for t in model.absorbed_tables}


def _profile_of(profiles: dict[str, Any], name: str) -> dict[str, Any]:
    p = profiles.get(name.lower())
    if not p:
        return {}
    return {
        "score": round(float(p.get("score", 0.0)), 4),
        "role": p.get("role"),
        "in_degree": p.get("in_degree"),
        "out_degree": p.get("out_degree"),
    }
