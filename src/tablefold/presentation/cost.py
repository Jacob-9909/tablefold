"""What a model costs, and the column rules that decide it.

Selection has to price an anchor *before* the model exists: a candidate that
brings two new tables at the price of forty fields is a worse buy than one
bringing two at the price of eight, and the greedy loop cannot see that unless
it can count fields it has not built yet.

So the predicates deciding what becomes a field live here rather than in
:mod:`tablefold.compose`, and both modules read them — compose to build the
fields, cluster to price them. Keeping one copy is the point: an estimate drawn
from different rules than the builder uses would drift silently, and the
selection would be optimising against a model nobody produces.

(:mod:`tablefold.compose` imports :mod:`tablefold.cluster` for ``Clustering``,
so the dependency cannot run the other way.)
"""

from __future__ import annotations

from tablefold.graph.graph import SchemaGraph
from tablefold.schema.ir import PhysicalColumn, PhysicalTable

# Columns that carry no business meaning at any grain.
NOISE_SUFFIXES = ("_hash", "_token", "_secret", "_password", "_salt")

# 자식 숫자형 컬럼 하나당 방출되는 집계 목록.
#
# `avg`는 의도적으로 뺐다. 모든 모델이 `<child>_count`를 갖고 있으므로 평균은
# `sum / count`로 계산된다 — 컨텍스트를 쓰면서 표현 가능한 질문은 하나도 늘리지
# 못하는 필드다. retail 픽스처에서 avg는 188개 필드 중 34개, 렌더된 레이어의
# 16%를 차지했다. `expand`는 여전히 avg를 지원하므로 필요한 레이어는 명시하면 된다.
NUMERIC_AGGREGATES = ("sum",)

# 자식 테이블 하나에서 집계할 숫자 컬럼 상한. 넓은 자식 테이블 하나가
# 모델의 다른 필드를 전부 밀어내지 못하게 한다.
MAX_AGGREGATED_COLUMNS_PER_CHILD = 3

# 서로 다른 두 값. 이전에는 하나의 이름과 기본값을 공유했다.
#
# MAX_MODEL_FIELDS — 모델 *하나*의 상한. 선택 단계가 후보 가격을 매기는 기준이다.
#   잘려나갈 필드를 가진 후보는 원본 컬럼 수만큼 비싸지 않기 때문이다.
# DEFAULT_FIELD_BUDGET — *레이어 전체*가 쓸 수 있는 예산. 한 번에 읽히는 단위가
#   레이어이므로 실제로 의미 있는 값은 이쪽이다. 모델별 한도만으로는 한 모델이
#   천장에 부딪히는 동안 다른 모델이 같은 크기의 몫을 86% 남기는 걸 막을 수 없다.
MAX_MODEL_FIELDS = 64
DEFAULT_FIELD_BUDGET = 200


def is_noise(column_name: str) -> bool:
    return any(column_name.lower().endswith(suffix) for suffix in NOISE_SUFFIXES)


def fk_columns(table: PhysicalTable, graph: SchemaGraph) -> set[str]:
    return {c.lower() for fk in graph.outgoing(table.name) for c in fk.from_columns}


def promotable_columns(
    table: PhysicalTable,
    graph: SchemaGraph,
    *,
    drop_primary_key: bool,
    drop_foreign_keys: bool = True,
) -> tuple[PhysicalColumn, ...]:
    """Columns of *table* that survive into a model as fields.

    Foreign keys go because the row they identify is promoted in their place.
    Primary keys go only when the table is being joined in — the anchor's own
    key is worth keeping, a lookup table's is not.
    """
    keys: set[str] = set()
    if drop_foreign_keys:
        keys |= fk_columns(table, graph)
    if drop_primary_key:
        keys |= {c.lower() for c in table.primary_key}

    return tuple(
        c for c in table.columns if not is_noise(c.name) and c.name.lower() not in keys
    )


def aggregatable_columns(
    table: PhysicalTable, graph: SchemaGraph
) -> tuple[PhysicalColumn, ...]:
    """Numeric columns of a child table that become aggregates at the parent."""
    keys = fk_columns(table, graph) | {c.lower() for c in table.primary_key}
    numeric = [c for c in table.columns if c.is_numeric and c.name.lower() not in keys]
    return tuple(numeric[:MAX_AGGREGATED_COLUMNS_PER_CHILD])


def estimate_fields(
    graph: SchemaGraph,
    anchor: str,
    *,
    max_hops: int,
    cap: int = MAX_MODEL_FIELDS,
) -> int:
    """Fields a model anchored on *anchor* would carry.

    An upper bound before name deduplication, which is what selection wants: it
    is exact for any model big enough to hit the cap, and only slightly high for
    the small ones, where a few duplicate names are the difference.
    """
    table = graph.schema.table(anchor)
    if table is None:
        return 0

    total = len(promotable_columns(table, graph, drop_primary_key=False))

    for target, _ in graph.walk_many_to_one(anchor, max_hops=max_hops):
        joined = graph.schema.table(target)
        if joined is not None:
            total += len(promotable_columns(joined, graph, drop_primary_key=True))

    for child, _ in graph.children(anchor):
        child_table = graph.schema.table(child)
        if child_table is not None:
            # COUNT 하나, 그리고 집계 가능한 컬럼마다 집계 하나씩.
            total += 1 + len(aggregatable_columns(child_table, graph)) * len(
                NUMERIC_AGGREGATES
            )

    return min(total, cap)
