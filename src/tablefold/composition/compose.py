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
    child_dimension_filters,
    filterable_columns,
    promotable_columns,
)
from tablefold.schema.ir import (
    Cardinality,
    FieldKind,
    FieldSource,
    JoinStep,
    LogicalField,
    LogicalLayer,
    LogicalModel,
    PhysicalColumn,
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

    expose_child_filters: bool = False
    """집계된 자식의 원본 컬럼을 필터 전용 필드로 노출할지.

    사전집계된 값에는 기간을 걸 수 없다 — ``SUM(SALES_AMT)`` 은 이미 전 기간을
    더한 뒤다. 이 옵션을 켜면 ``F_SALES_YYYYMMDD`` 같은 필드가 생기고,
    ``expand`` 가 거기 걸린 조건을 집계 서브쿼리 *안* 으로 밀어 넣는다.

    기본값이 꺼짐인 이유는 값이 아니라 통로일 뿐인 필드가 자식 컬럼 수만큼
    늘어나기 때문이다. 차원을 앵커로 삼아 여러 팩트를 나란히 놓는 질의처럼,
    사전집계에 조건을 걸어야 하는 경우에만 켠다.
    """

    prefix_joined_fields: bool = True
    """인라인된 컬럼에 원본 테이블 이름을 접두사로 붙일지.

    켜 두면 ``customers.email`` 이 ``customer_email`` 이 된다. 넓은 모델에서
    ``email`` 만 있으면 누구의 이메일인지 모르므로 보통은 이쪽이 낫다.

    끄면 원래 컬럼 이름을 그대로 쓰고, 충돌할 때만 ``_disambiguate`` 가 경로로
    구분한다. 테이블 이름 자체가 사람이 읽을 말이 아닌 웨어하우스 스키마에서
    필요하다 — ``D_SA_ORG.HEAD_NM`` 은 ``d_sa_org_HEAD_NM`` 이 되는데, 이 스키마의
    실제 질의는 ``HEAD_NM`` 을 그대로 쓴다.
    """


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
                (2, f) for f in _aggregated_fields(child_table, step, graph, opts)
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
    # 참조 키가 역할을 담고 있으면 그것을 접두사로 쓴다. ``buyer_id`` 로 간
    # ``users.username`` 은 ``user_username`` 이 아니라 ``buyer_username`` 이다.
    # 같은 대상으로 가는 경로가 둘일 때 한쪽만 역할 이름을 갖는 비대칭을 없앤다.
    prefix = (path and _join_role(path[-1])) or singular(table.name)
    return [
        LogicalField(
            name=(
                _prefixed(prefix, column.name)
                if opts.prefix_joined_fields
                else column.name
            ),
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
    table: PhysicalTable, step: JoinStep, graph: SchemaGraph, opts: ComposeOptions
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

    measures = aggregatable_columns(table, graph)
    for column in measures:
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

    if opts.expose_child_filters:
        fields.extend(_filter_fields(table, step, graph, measures, prefix))
    return fields


def _filter_fields(
    table: PhysicalTable,
    step: JoinStep,
    graph: SchemaGraph,
    measures: tuple[PhysicalColumn, ...],
    prefix: str,
) -> list[LogicalField]:
    """집계된 자식에 조건을 걸 수 있게 원본 컬럼을 필터 전용으로 노출한다.

    측정값과 조인 키는 제외한다. 측정값은 이미 집계로 나가 있고, 조인 키는
    앵커 쪽에 같은 값이 있다. 남는 것은 기간·통화·구분 같은 것들이고, 그게
    사전집계에 조건을 거는 유일한 통로다.
    """
    # 어느 컬럼이 필터가 되는지의 규칙은 ``cost`` 에 한 벌만 둔다. 선택 단계가
    # 후보 가격을 매길 때 같은 규칙을 읽어야 추정과 실제가 어긋나지 않는다.
    fields = [
        LogicalField(
            name=f"{prefix}_{column.name}",
            type=column.type,
            source=FieldSource(
                kind=FieldKind.AGGREGATED,
                table=table.name,
                column=column.name,
                path=(step,),
            ),
            description=f"Filters the {table.name} rows folded into this row.",
            filter_only=True,
        )
        for column in filterable_columns(table, graph, step, measures)
    ]

    # 조건이 자식 자신이 아니라 자식이 가리키는 차원에 걸리는 경우가 있다.
    # "매출액 계정만" 은 ``F_PL`` 이 아니라 ``D_PL_ACCT.PL_ACCT2_NM`` 의 이야기다.
    # 경로를 두 단으로 두면 ``expand`` 가 집계 서브쿼리 안에서 그 차원까지 조인해
    # 조건을 건다.
    for dim_name, columns in child_dimension_filters(table, graph):
        dim_step = next(
            (
                JoinStep(
                    from_table=table.name,
                    from_columns=fk.from_columns,
                    to_table=fk.to_table,
                    to_columns=fk.to_columns,
                    cardinality=Cardinality.MANY_TO_ONE,
                )
                for fk in graph.outgoing(table.name)
                if fk.to_table.lower() == dim_name.lower()
            ),
            None,
        )
        if dim_step is None:
            continue
        fields.extend(
            LogicalField(
                name=f"{prefix}_{column.name}",
                type=column.type,
                source=FieldSource(
                    kind=FieldKind.AGGREGATED,
                    table=dim_name,
                    column=column.name,
                    path=(step, dim_step),
                ),
                description=(
                    f"Filters the {table.name} rows by {dim_name}.{column.name}."
                ),
                filter_only=True,
            )
            for column in columns
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


# 참조 컬럼 이름에서 떼면 역할만 남는 꼬리표. ``seller_id`` → ``seller``.
_ROLE_SUFFIXES = ("_id", "_cd", "_code", "_key", "_no", "_num", "_fk")


def _disambiguate(field: LogicalField) -> str:
    """조인 경로를 이용해 이름 충돌을 구분한다.

    구분 정보는 대개 대상 테이블이 아니라 **어느 키로 갔는가** 에 있다.
    ``orders.buyer_id`` 와 ``orders.seller_id`` 는 둘 다 ``users`` 로 가므로
    출발 테이블 이름(``order``)을 붙여 봐야 ``order_user_username`` 이라는, 어느
    쪽인지 알려 주지 않는 이름이 된다. 키에서 역할을 떼면 ``seller_username`` 이
    되고, 이건 사람이 붙였을 이름과 같다.

    키에서 아무것도 못 얻으면 경로의 출발 테이블로 되돌아간다 — ``addresses``를
    거쳐 온 ``countries.name``과 ``stores``를 거쳐 온 것을 구분하는 경우다.
    """
    source = field.source
    if not source.path:
        return f"{singular(source.table)}_{source.column}"

    last = source.path[-1]
    role = _join_role(last)

    if role and last.cardinality is Cardinality.ONE_TO_MANY:
        # 집계 필드의 이름에는 컬럼이 아니라 집계가 들어 있고(``..._count``),
        # ``source.column`` 은 ``*`` 일 수도 있다. 이름 앞에 역할만 붙인다.
        return f"{role}_{field.name}"
    if role:
        return f"{role}_{source.column}"

    via = singular(last.from_table)
    return f"{via}_{field.name}"


def _join_role(step: JoinStep) -> str | None:
    """이 조인 단계를 다른 단계와 구분하는 역할 이름.

    구분 정보는 대상 테이블이 아니라 **어느 키로 갔는가** 에 있다. 방향에 따라
    보아야 할 쪽이 다르다 — 정방향은 앵커가 들고 있는 키(``seller_id``), 역방향은
    자식이 들고 있는 키(``dst_order_id``)가 그 역할을 담는다.

    키에서 얻은 이름이 대상 테이블 이름과 같으면 아무것도 구분해 주지 못하므로
    ``None`` 을 돌려주고, 호출부가 경로로 되돌아가게 한다.
    """
    columns = (
        step.to_columns
        if step.cardinality is Cardinality.ONE_TO_MANY
        else step.from_columns
    )
    if len(columns) != 1:
        return None

    lowered = columns[0].lower()
    for suffix in _ROLE_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix):
            role = lowered[: -len(suffix)]
            related = {singular(step.to_table), singular(step.from_table)}
            return role if role not in related else None
    return None


def _prefixed(prefix: str, column: str) -> str:
    """승격된 컬럼에 접두사를 붙인다. 이미 그렇게 읽히면 그대로 둔다.

    orders 위로 승격된 ``customers.customer_id``는 ``customer_customer_id``가
    아니라 ``customer_id``로 남아야 한다.
    """
    lowered = column.lower()
    if lowered.startswith(f"{prefix}_") or lowered == prefix:
        return column
    return f"{prefix}_{column}"
