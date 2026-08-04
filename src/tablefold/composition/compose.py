"""앵커 하나당 와이드 논리 모델 하나를 만든다.

모델의 입도(grain)는 정확히 앵커 테이블의 행당 한 행이다. 그 입도에 존재할 수 있는
필드는 세 종류이고, 이 구분은 분류 체계가 아니라 정합성 제약이다:

* **base** — 앵커 자신의 컬럼.
* **joined** — 외래 키를 *정방향*으로 따라가 도달한 컬럼. 최대 한 행만 매칭되므로
  인라인해도 행 수가 변하지 않는다.
* **aggregated** — 자식 테이블의 컬럼. 행이 늘어나므로 집계를 통해서만 존재할 수
  있다. 인라인하면 앵커의 행이 뻥튀기되고, 쿼리의 모든 합계가 조용히 틀어진다.

필드 수에는 예산이 걸리며, 예산은 모델별이 아니라 **레이어 전체**에 걸린다. 한 번에
읽히는 단위가 폴드 결과 전체이므로 예산의 단위도 그래야 한다. 모델별 한도로는 필요
없는 모델의 여유분을 필요한 모델로 옮길 수 없다 — retail 픽스처에서 ``orders``가
자기 천장에서 잘려나가는 동안 ``tax_rates``는 동일한 할당량의 14%만 썼다.

필드는 우선순위 순으로 편입된다. 모든 모델의 자체 컬럼이 먼저, 다음이 1홉 조인과
자식 집계, 그다음이 점점 더 먼 조인. 같은 우선순위 안에서는 모델들이 번갈아
가져가므로, 후보가 많은 모델 하나가 나머지를 굶기지 못한다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from tablefold.clustering.cluster import Clustering
from tablefold.graph.graph import SchemaGraph
from tablefold.presentation.cost import (
    DEFAULT_FIELD_BUDGET,
    MAX_MODEL_FIELDS,
    NUMERIC_AGGREGATES,
    aggregatable_columns,
    promotable_columns,
)
from tablefold.schema.ir import (
    FieldKind,
    FieldSource,
    JoinStep,
    LogicalField,
    LogicalLayer,
    LogicalModel,
    PhysicalTable,
    plural,
    singular,
)


@dataclass(frozen=True)
class ComposeOptions:
    max_hops: int = 3

    field_budget: int = DEFAULT_FIELD_BUDGET
    """레이어 **전체**가 쓸 수 있는 필드 수. 모델 하나당이 아니다."""

    max_model_fields: int = MAX_MODEL_FIELDS
    """모델 하나의 상한. 한 앵커가 예산 전체를 가져가지 못하게 한다."""

    include_foreign_key_columns: bool = False
    include_aggregates: bool = True


def compose(
    graph: SchemaGraph,
    clustering: Clustering,
    *,
    options: ComposeOptions | None = None,
) -> LogicalLayer:
    """각 주제 영역(SubjectArea)을 와이드 논리 모델 하나로 만든다."""
    opts = options or ComposeOptions()
    drafts = tuple(
        _draft(graph, area.anchor, opts, name=area.name)
        for area in clustering.areas
        if graph.schema.table(area.anchor) is not None
    )
    notes = tuple(f"uncovered: {name}" for name in clustering.unassigned)
    return LogicalLayer(
        models=_allocate(drafts, opts),
        source_table_count=len(graph.schema.tables),
        source_column_count=sum(len(t.columns) for t in graph.schema.tables),
        covered_table_count=clustering.covered_table_count,
        stop_reason=clustering.stop_reason.value,
        selector=clustering.selector,
        notes=notes,
    )


@dataclass(frozen=True)
class _Draft:
    """예산을 쓰기 전, 한 모델이 가질 *수 있는* 모든 필드를 담은 초안."""

    name: str
    base_table: str
    absorbed: tuple[str, ...]
    description: str
    candidates: tuple[tuple[int, LogicalField], ...]
    """``(우선순위, 필드)``. 값이 작을수록 먼저 편입된다."""


def _draft(
    graph: SchemaGraph, anchor: str, opts: ComposeOptions, *, name: str | None = None
) -> _Draft:
    """*anchor*를 기준으로 와이드 모델 초안을 만든다.

    ``name``은 모델의 이름이며 앵커의 테이블명과 같을 필요는 없다. 도메인을 이해하는
    셀렉터라면 ``invoice_lines``에 앵커링된 모델을 ``billing``이라 부를 수 있다.
    베이스 테이블은 영향받지 않는다 — 입도는 여전히 앵커의 행당 한 행이고, 확장은
    여전히 물리 테이블에서 읽는다.
    """
    table = graph.schema.table(anchor)
    assert table is not None  # 호출부에서 보장

    candidates: list[tuple[int, LogicalField]] = []
    absorbed: set[str] = {table.name}

    candidates.extend((0, f) for f in _base_fields(table, graph, opts))

    for target, path in graph.walk_many_to_one(anchor, max_hops=opts.max_hops):
        target_table = graph.schema.table(target)
        if target_table is None:
            continue
        absorbed.add(target_table.name)
        # 1홉은 앵커의 직접 속성이라 자체 컬럼 바로 다음 순위이고,
        # 홉이 늘수록 점점 더 부차적이다.
        priority = 1 + len(path)
        candidates.extend(
            (priority, f) for f in _joined_fields(target_table, path, graph, opts)
        )

    if opts.include_aggregates:
        for child, step in graph.children(anchor):
            child_table = graph.schema.table(child)
            if child_table is None:
                continue
            absorbed.add(child_table.name)
            # 집계는 2홉 조인보다 앞선다. 자식 행 수가 먼 곳의 라벨보다
            # 답이 되는 경우가 많다.
            candidates.extend(
                (2, f) for f in _aggregated_fields(child_table, step, graph)
            )

    return _Draft(
        name=name or table.name,
        base_table=table.name,
        absorbed=tuple(sorted(absorbed - {table.name})),
        description=(
            f"One row per {singular(table.name)}. "
            f"Folds {len(absorbed)} physical tables."
        ),
        candidates=tuple(candidates),
    )


# ── field builders ────────────────────────────────────────────────────────────


def _base_fields(
    table: PhysicalTable, graph: SchemaGraph, opts: ComposeOptions
) -> list[LogicalField]:
    # 외래 키는 기본적으로 제외한다. 참조 대상 행의 속성이 그 자리에 승격되므로
    # 원본 키는 LLM 컨텍스트에서 의미 없는 정수가 된다.
    columns = promotable_columns(
        table,
        graph,
        drop_primary_key=False,
        drop_foreign_keys=not opts.include_foreign_key_columns,
    )
    return [
        LogicalField(
            name=column.name,
            type=column.type,
            source=FieldSource(
                kind=FieldKind.BASE, table=table.name, column=column.name
            ),
            description=column.comment,
        )
        for column in columns
    ]


def _joined_fields(
    table: PhysicalTable,
    path: tuple[JoinStep, ...],
    graph: SchemaGraph,
    opts: ComposeOptions,
) -> list[LogicalField]:
    """참조된 테이블의 서술적 컬럼을 앵커 위로 승격한다."""
    prefix = singular(table.name)
    return [
        LogicalField(
            name=_prefixed(prefix, column.name),
            type=column.type,
            source=FieldSource(
                kind=FieldKind.JOINED,
                table=table.name,
                column=column.name,
                path=path,
            ),
            description=column.comment,
        )
        for column in promotable_columns(table, graph, drop_primary_key=True)
    ]


def _aggregated_fields(
    table: PhysicalTable, step: JoinStep, graph: SchemaGraph
) -> list[LogicalField]:
    """자식 테이블을 부모 입도의 측정값으로 축소한다."""
    prefix = plural(table.name)

    fields: list[LogicalField] = [
        LogicalField(
            name=f"{prefix}_count",
            type="bigint",
            source=FieldSource(
                kind=FieldKind.AGGREGATED,
                table=table.name,
                column="*",
                path=(step,),
                aggregate="count",
            ),
            description=f"Number of {table.name} rows for this row.",
        )
    ]

    for column in aggregatable_columns(table, graph):
        for aggregate in NUMERIC_AGGREGATES:
            fields.append(
                LogicalField(
                    name=f"{prefix}_{column.name}_{aggregate}",
                    type=column.type,
                    source=FieldSource(
                        kind=FieldKind.AGGREGATED,
                        table=table.name,
                        column=column.name,
                        path=(step,),
                        aggregate=aggregate,
                    ),
                )
            )
    return fields


# ── budget and naming ─────────────────────────────────────────────────────────


def _allocate(
    drafts: tuple[_Draft, ...], opts: ComposeOptions
) -> tuple[LogicalModel, ...]:
    """레이어의 필드 예산을 모든 모델에 한 번에 배분한다.

    편입 순서는 우선순위 — 모든 모델의 자체 컬럼, 다음이 1홉 조인과 자식 집계,
    그다음이 더 먼 조인. 같은 우선순위 안에서는 모델들이 번갈아 가져가므로,
    후보가 200개인 모델이 작은 모델이 자기를 설명하기도 전에 예산을 비우지 못한다.
    """
    resolved = [_deduplicate(d.candidates) for d in drafts]

    queue: list[tuple[int, int, int, LogicalField]] = []
    for index, fields in enumerate(resolved):
        turn: dict[int, int] = {}
        for priority, fld in fields:
            position = turn.get(priority, 0)
            turn[priority] = position + 1
            queue.append((priority, position, index, fld))
    queue.sort(key=lambda item: item[:3])

    kept: list[list[LogicalField]] = [[] for _ in drafts]
    spent = 0
    for _, _, index, fld in queue:
        if spent >= opts.field_budget:
            break
        if len(kept[index]) >= opts.max_model_fields:
            continue
        kept[index].append(fld)
        spent += 1

    return tuple(
        LogicalModel(
            name=draft.name,
            base_table=draft.base_table,
            fields=tuple(fields),
            absorbed_tables=draft.absorbed,
            description=draft.description,
        )
        for draft, fields in zip(drafts, kept, strict=True)
    )


def _deduplicate(
    candidates: tuple[tuple[int, LogicalField], ...],
) -> list[tuple[int, LogicalField]]:
    """한 모델 안의 이름 충돌을 해결한다. 우선순위 순서는 유지된다."""
    kept: list[tuple[int, LogicalField]] = []
    seen: set[str] = set()

    for priority, field in sorted(candidates, key=lambda pair: pair[0]):
        name = field.name
        if name.lower() in seen:
            name = _disambiguate(field)
            if name.lower() in seen:
                continue
            field = replace(field, name=name)
        seen.add(name.lower())
        kept.append((priority, field))

    return kept


def _disambiguate(field: LogicalField) -> str:
    """조인 경로를 이용해 이름 충돌을 구분한다.

    ``addresses``를 거쳐 온 ``countries.name``과 ``stores``를 거쳐 온 것은 이름만
    같은 다른 컬럼이고, 둘을 구분하는 것은 경로뿐이므로 경로가 이름에 들어간다.
    """
    source = field.source
    if not source.path:
        return f"{singular(source.table)}_{source.column}"
    via = singular(source.path[-1].from_table)
    return f"{via}_{field.name}"


def _prefixed(prefix: str, column: str) -> str:
    """승격된 컬럼에 접두사를 붙인다. 이미 그렇게 읽히면 그대로 둔다.

    orders 위로 승격된 ``customers.customer_id``는 ``customer_customer_id``가
    아니라 ``customer_id``로 남아야 한다.
    """
    lowered = column.lower()
    if lowered.startswith(f"{prefix}_") or lowered == prefix:
        return column
    return f"{prefix}_{column}"
