"""What a model costs, and the column rules that decide it.

Selection has to price an anchor *before* the model exists: a candidate that
brings two new tables at the price of forty fields is a worse buy than one
bringing two at the price of eight, and the greedy loop cannot see that unless
it can count fields it has not built yet.

So the predicates deciding what becomes a field live here rather than in
:mod:`tablefold.build.compose`, and both modules read them — compose to build the
fields, cluster to price them. Keeping one copy is the point: an estimate drawn
from different rules than the builder uses would drift silently, and the
selection would be optimising against a model nobody produces.

이 모듈이 ``build`` 가 아니라 ``choose`` 에 있는 이유가 그것이다. 선택이 가격을
알아야 하고, ``build`` 는 ``choose`` 의 ``Clustering`` 을 읽으므로 의존이 반대로
흐를 수 없다.
"""

from __future__ import annotations

from tablefold.ir import PhysicalColumn, PhysicalTable
from tablefold.relate.graph import SchemaGraph

# Columns that carry no business meaning at any grain.
NOISE_SUFFIXES = ("_hash", "_token", "_secret", "_password", "_salt")

# 적재 메타데이터. 어느 테이블에나 있고 어떤 질문에도 답하지 않는다.
#
# NL2SQL 스키마에서 이것들이 155개 참조 컬럼 중 20개를 차지했고, 그중 10개는
# 데이터가 전부 NULL 이었다. 필드 예산을 쓰면서 읽는 쪽에는 고를 이유가 없는
# 선택지만 늘린다 — 골드셋에서 실제로 값이 빈 컬럼이 답으로 골라졌다.
NOISE_NAMES = frozenset(
    {
        "load_dt", "load_user", "load_date", "load_time",
        "etl_dt", "etl_id", "etl_user", "etl_date",
        "create_dt", "update_dt", "insert_dt", "modify_dt",
        "reg_dt", "chg_dt", "dw_load_dt",
    }
)

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
    lowered = column_name.lower()
    if lowered in NOISE_NAMES:
        return True
    return any(lowered.endswith(suffix) for suffix in NOISE_SUFFIXES)


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
    include_aggregates: bool = True,
    expose_child_filters: bool = False,
) -> int:
    """Fields a model anchored on *anchor* would carry.

    An upper bound before name deduplication, which is what selection wants: it
    is exact for any model big enough to hit the cap, and only slightly high for
    the small ones, where a few duplicate names are the difference.

    ``include_aggregates`` 와 ``expose_child_filters`` 는 :class:`ComposeOptions`
    의 같은 이름 옵션과 같은 뜻이며, 반드시 같은 값으로 넘겨야 한다. 이 모듈이
    존재하는 이유가 추정과 실제가 같은 규칙을 읽게 하는 것인데, 이 두 옵션을
    빼먹은 동안 추정이 실제의 절반도 안 됐다 — retail 의 ``customers`` 는
    27 로 값이 매겨졌지만 실제로는 64 개 필드를 냈다. 심사 규칙
    (``estimated_fields / gain``) 이 그만큼 싸게 통과시켰다는 뜻이다.
    """
    table = graph.schema.table(anchor)
    if table is None:
        return 0

    total = len(promotable_columns(table, graph, drop_primary_key=False))

    for target, _ in graph.walk_many_to_one(anchor, max_hops=max_hops):
        joined = graph.schema.table(target)
        if joined is not None:
            total += len(promotable_columns(joined, graph, drop_primary_key=True))

    if include_aggregates:
        for child, step in graph.children(anchor):
            child_table = graph.schema.table(child)
            if child_table is None:
                continue
            measures = aggregatable_columns(child_table, graph)
            # COUNT 하나, 그리고 집계 가능한 컬럼마다 집계 하나씩.
            total += 1 + len(measures) * len(NUMERIC_AGGREGATES)
            if expose_child_filters:
                total += len(filterable_columns(child_table, graph, step, measures))
                total += sum(
                    len(cols) for _, cols in child_dimension_filters(child_table, graph)
                )

    return min(total, cap)


# 집계된 자식의 차원 하나에서 조건으로 노출할 컬럼 상한.
#
# 자식마다 차원이 여럿이고 차원마다 컬럼이 여럿이라 그냥 두면 곱으로 늘어난다.
# 조건에 실제로 쓰이는 것은 이름표 몇 개뿐이므로 앞쪽만 취한다.
MAX_CHILD_DIMENSION_FILTERS = 4


def child_dimension_filters(
    child: PhysicalTable, graph: SchemaGraph
) -> tuple[tuple[str, tuple[PhysicalColumn, ...]], ...]:
    """집계된 자식이 참조하는 차원과, 그 차원에서 조건으로 쓸 컬럼들.

    ``D_FI_ORG`` 를 앵커로 삼으면 ``F_PL`` 이 사전집계로 붙는다. 그런데 "매출액
    계정만" 같은 조건은 ``F_PL`` 자신이 아니라 ``F_PL`` 이 가리키는
    ``D_PL_ACCT.PL_ACCT2_NM`` 에 걸린다. 그 통로가 없으면 재무 주제의 질문이
    통째로 답이 안 된다 — 골드셋 FI_0001 이 정확히 여기서 실패했다.

    한 홉만 따라간다. 두 홉을 더 가면 집계 서브쿼리 안의 조인이 깊어지는데,
    조건에 그만큼 먼 컬럼이 쓰이는 경우가 드물다.
    """
    found: list[tuple[str, tuple[PhysicalColumn, ...]]] = []
    for fk in graph.outgoing(child.name):
        dim = graph.schema.table(fk.to_table)
        if dim is None or dim.name.lower() == child.name.lower():
            continue
        columns = promotable_columns(dim, graph, drop_primary_key=True)
        if columns:
            found.append((dim.name, columns[:MAX_CHILD_DIMENSION_FILTERS]))
    return tuple(found)


def filterable_columns(
    table: PhysicalTable,
    graph: SchemaGraph,
    step: object,
    measures: tuple[PhysicalColumn, ...],
) -> tuple[PhysicalColumn, ...]:
    """집계된 자식에서 필터 전용 필드가 될 컬럼.

    ``compose._filter_fields`` 와 같은 규칙이다. 한 벌만 두는 것이 이 모듈의
    존재 이유이므로 규칙은 여기 있고 양쪽이 읽는다.
    """
    aggregated = {c.name.lower() for c in measures}
    join_key = {c.lower() for c in getattr(step, "to_columns", ())}
    return tuple(
        c
        for c in promotable_columns(
            table,
            graph,
            drop_primary_key=len(table.primary_key) == 1,
            drop_foreign_keys=False,
        )
        if c.name.lower() not in aggregated and c.name.lower() not in join_key
    )
