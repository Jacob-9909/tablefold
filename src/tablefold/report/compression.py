"""컬럼이 어떻게 접혔는지 센다. "얼마나 잃었나"는 fidelity 모듈이 잰다.

화면의 물음은 두 개다. "잘 묶였나"는 반영도 게이지가 답하고, "**어디서 왔나**"는
이 모듈이 답한다. 모델 하나를 열면 어느 물리 표의 컬럼 몇 개가 기본값으로
들어왔고, 어느 표는 조인을 거쳐 붙었고, 어느 표는 여러 줄을 합계로 접었다는
흐름이 나와야 한다. 그 흐름 없이 게이지만 있으면 숫자는 믿기 어렵다 — 근거가
보이지 않기 때문이다.

여기서 세는 것은 전부 레이어가 이미 들고 있는 출처(``FieldSource``)다. 새로운
사실을 만들지 않는다 — 같은 컬럼을 다르게 설명하면 화면과 데이터가 싸운다.
"""

from __future__ import annotations

from tablefold.ir import FieldKind, LogicalLayer

_KIND_ORDER = (
    FieldKind.BASE,
    FieldKind.JOINED,
    FieldKind.AGGREGATED,
)


def measure(layer: LogicalLayer) -> dict:
    """레이어의 압축 흐름을 직렬화한다.

    모델마다 **어느 표가 몇 개의 컬럼을 넣었는지**(flows)와 전체 비율을 돌려준다.
    ``physical_columns`` 는 모델 안에서 서로 다른 ``(표, 컬럼)`` 짝의 수다 — 한
    컬럼이 두 모델에 나뉘어 살 때 두 번 세면 비율이 부풀어 1:1 압축을 2:1 처럼
    보이게 한다.
    """
    models = []
    distinct_pairs: set[tuple[str, str]] = set()
    total_fields = 0

    for model in layer.models:
        by_table: dict[str, dict[str, int]] = {}
        hops_max = 0
        agg_children: set[str] = set()

        for f in model.fields:
            kind = _bucket(f)
            src = f.source
            entry = by_table.setdefault(
                src.table,
                {"columns": 0, "base": 0, "joined": 0, "aggregated": 0, "filter": 0},
            )
            entry["columns"] += 1
            entry[kind] += 1
            distinct_pairs.add((src.table.lower(), src.column.lower()))
            hops_max = max(hops_max, src.hops)
            if kind == "aggregated":
                agg_children.add(src.table)

        flows = sorted(
            (
                {
                    "table": table,
                    "columns": v["columns"],
                    "kinds": {
                        k: v[k]
                        for k in ("base", "joined", "aggregated", "filter")
                        if v[k]
                    },
                    # 이 표가 모델에 들어온 방식. 화면 문장이 이걸 고른다.
                    "role": _role(v, model.base_table.lower(), table.lower()),
                }
                for table, v in by_table.items()
            ),
            key=lambda x: (-x["columns"], x["table"].lower()),
        )

        models.append(
            {
                "name": model.name,
                "base_table": model.base_table,
                "logical_fields": len(model.fields),
                "source_tables": len(by_table),
                "max_hops": hops_max,
                "aggregated_children": len(agg_children),
                "by_kind": count_kinds(model),
                "flows": flows,
            }
        )
        total_fields += len(model.fields)

    physical = len(distinct_pairs)
    return {
        "physical_columns": physical,
        "logical_fields": total_fields,
        # 분모가 0 이면 압축이 아니라 빈 레이어다. 1.0 은 "압축 없음"의 자리다.
        "ratio": round(physical / total_fields, 2) if total_fields else 1.0,
        "models": models,
    }


def count_kinds(model) -> dict[str, int]:
    """모델의 필드를 네 통(桶)으로 세어 돌려준다."""
    out = {"base": 0, "joined": 0, "aggregated": 0, "filter": 0}
    for f in model.fields:
        out[_bucket(f)] += 1
    return out


def _bucket(f) -> str:
    """필터 전용은 종류보다 쓰임이 우선이다. WHERE 에만 쓰이는 필드를 base 로
    세면 "값을 꺼낼 수 있는 컬럼"이 과대계된다."""
    if f.filter_only:
        return "filter"
    return f.source.kind.value


def _role(entry: dict, base_table: str, table: str) -> str:
    """한 표의 기여 모양. base 가 우세하면 기준, 합계가 있으면 집계, 나머지는 붙임.

    기준 표 자체는 무조건 '기준'이다 — 필드 예산에 눌려 base 보다 joined 가 많아져도
    모델의 입도를 정한 것은 이 표이므로.
    """
    if table == base_table:
        return "anchor"
    if entry.get("aggregated"):
        return "aggregated"
    if entry.get("base") and not entry.get("joined"):
        return "inlined"
    return "inlined"
