"""논리 모델(Logical Model)을 타깃으로 작성된 SQL을
실제 데이터베이스에서 실행 가능한 SQL로 확장(Expand/Rewrite)합니다.

쿼리가 와이드 모델 및 해당 필드를 참조하면, 이를 물리 테이블로 재구성하는
CTE(Common Table Expression)를 생성하고 해당 CTE를 조회하도록 SQL 쿼리를
다시 작성(Rewrite)합니다.

핵심 원칙 2가지:

**1. 데이터 입도(Grain) 보존**: N:1 조인은 직접 인라인 조인됩니다.
1:N 자식 테이블은 *절대* 직접 인라인 조인되지 않고, 서브쿼리에서 먼저
`GROUP BY`로 선집계(Pre-aggregation)한 후 조인됩니다.
자식 테이블을 직접 조인할 경우 부모 행 수가 뻥튀기되어
모든 집계 연산(SUM 등)이 오염되는 심각한 오류가 발생하기 때문입니다.

**2. 필요한 조인만 생성 (Join Pruning)**: 논리 모델이 15개 테이블을
포함하더라도, 쿼리가 그중 3개 필드만 사용하면 해당 3개 필드에 필요한
최소한의 조인 구문만 CTE 내에 생성하여 방출합니다.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from tablefold.graph.graph import SchemaGraph
from tablefold.schema.ir import (
    FieldKind,
    JoinStep,
    LogicalField,
    LogicalLayer,
    LogicalModel,
)

_BASE_ALIAS = "base"

# CTEs are named with this prefix rather than the model's own name. A model is
# usually named after its anchor table, and a CTE named `orders` whose body
# reads `FROM orders` is a self-reference — the physical table becomes
# unreachable and the query either errors or silently reads the CTE. Renaming
# the CTE and aliasing it back at the call site keeps both names addressable.
_CTE_PREFIX = "tf__"


class ExpansionError(Exception):
    """The query cannot be expanded against the given logical layer."""


@dataclass(frozen=True)
class ExpansionResult:
    sql: str
    models_used: tuple[str, ...]
    fields_used: tuple[str, ...]
    joins_emitted: int
    joins_available: int

    @property
    def joins_pruned(self) -> int:
        return self.joins_available - self.joins_emitted


def expand(
    sql: str,
    layer: LogicalLayer,
    graph: SchemaGraph,
    *,
    dialect: str = "postgres",
    pretty: bool = True,
) -> ExpansionResult:
    """논리 모델을 참조하는 *sql*을
    실제 물리 테이블 대상 구문으로 다시 재작성(Expand)합니다.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
        raise ExpansionError(f"could not parse SQL: {exc}") from exc

    referenced = _referenced_models(statement, layer)
    if not referenced:
        raise ExpansionError(
            "query references no logical model; "
            f"known models: {', '.join(m.name for m in layer.models)}"
        )

    mentioned = _mentioned_columns(statement)
    selects_star = _selects_star(statement)

    ctes: list[tuple[str, exp.Select]] = []
    used_fields: list[str] = []
    emitted = 0
    available = 0

    unknown = _unknown_columns(mentioned, referenced)

    for model in referenced:
        fields = _required_fields(model, mentioned, star=selects_star)
        if not fields and unknown:
            raise ExpansionError(
                f"query references model '{model.name}' but none of its fields; "
                f"unknown: {', '.join(sorted(unknown))}; "
                f"available: {', '.join(model.field_names[:12])}…"
            )
        select, join_count = _build_model_select(model, graph, fields)
        ctes.append((model.name, select))
        used_fields.extend(f"{model.name}.{f.name}" for f in fields)
        emitted += join_count
        available += _available_join_count(model)

    rewritten = _rebind_model_references(statement, referenced)
    for name, select in ctes:
        rewritten = rewritten.with_(_cte_name(name), as_=select, copy=False)

    return ExpansionResult(
        sql=rewritten.sql(dialect=dialect, pretty=pretty),
        models_used=tuple(m.name for m in referenced),
        fields_used=tuple(used_fields),
        joins_emitted=emitted,
        joins_available=available,
    )


# ── query inspection ──────────────────────────────────────────────────────────


def _referenced_models(
    statement: exp.Expression, layer: LogicalLayer
) -> tuple[LogicalModel, ...]:
    found: list[LogicalModel] = []
    for table in statement.find_all(exp.Table):
        model = layer.model(table.name)
        if model is not None and all(m.name != model.name for m in found):
            found.append(model)
    return tuple(found)


def _mentioned_columns(statement: exp.Expression) -> frozenset[str]:
    return frozenset(
        column.name.lower() for column in statement.find_all(exp.Column) if column.name
    )


def _selects_star(statement: exp.Expression) -> bool:
    """True only for ``SELECT *`` / ``SELECT t.*``.

    Scanning for any ``Star`` node would also fire on ``COUNT(*)``, which
    appears in most real aggregate queries and would silently disable field
    pruning for all of them.
    """
    for select in statement.find_all(exp.Select):
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                return True
            if isinstance(projection, exp.Column) and isinstance(
                projection.this, exp.Star
            ):
                return True
    return False


def _required_fields(
    model: LogicalModel, mentioned: frozenset[str], *, star: bool
) -> tuple[LogicalField, ...]:
    if star:
        return model.fields
    return tuple(f for f in model.fields if f.name.lower() in mentioned)


def _unknown_columns(
    mentioned: frozenset[str], models: tuple[LogicalModel, ...]
) -> frozenset[str]:
    known = {name.lower() for model in models for name in model.field_names}
    return frozenset(mentioned - known)


def _rebind_model_references(
    statement: exp.Expression, models: tuple[LogicalModel, ...]
) -> exp.Expression:
    """Point each model reference at its CTE while keeping the model's name.

    ``FROM orders`` becomes ``FROM tf__orders AS orders``, so column references
    written against the model still resolve and an explicit alias the caller
    wrote is left alone.
    """
    by_name = {model.name.lower(): model for model in models}
    rewritten = statement.copy()

    for table in rewritten.find_all(exp.Table):
        model = by_name.get(table.name.lower())
        if model is None:
            continue
        table.set("this", exp.to_identifier(_cte_name(model.name)))
        table.set("db", None)
        table.set("catalog", None)
        if table.alias is None or table.alias == "":
            table.set("alias", exp.TableAlias(this=exp.to_identifier(model.name)))

    return rewritten


def _cte_name(model_name: str) -> str:
    return f"{_CTE_PREFIX}{model_name}"


# ── model expansion ───────────────────────────────────────────────────────────


def _build_model_select(
    model: LogicalModel, graph: SchemaGraph, fields: tuple[LogicalField, ...]
) -> tuple[exp.Select, int]:
    base = graph.schema.table(model.base_table)
    if base is None:
        raise ExpansionError(f"base table '{model.base_table}' is not in the schema")

    projections: list[exp.Expression] = []
    join_paths: dict[tuple[str, ...], tuple[JoinStep, ...]] = {}
    child_steps: dict[str, JoinStep] = {}
    child_fields: dict[str, list[LogicalField]] = {}

    for field in fields:
        source = field.source

        if source.kind is FieldKind.BASE:
            projections.append(
                _aliased(_column(_BASE_ALIAS, source.column), field.name)
            )

        elif source.kind is FieldKind.JOINED:
            # Register every prefix of the path — reaching a two-hop table
            # requires the one-hop table to be joined first.
            for depth in range(1, len(source.path) + 1):
                prefix = source.path[:depth]
                join_paths.setdefault(_path_key(prefix), prefix)
            alias = _path_alias(source.path)
            projections.append(_aliased(_column(alias, source.column), field.name))

        elif source.kind is FieldKind.AGGREGATED:
            step = source.path[0]
            child_steps.setdefault(step.to_table, step)
            child_fields.setdefault(step.to_table, []).append(field)
            alias = _aggregate_alias(step.to_table)
            projections.append(_aliased(_column(alias, field.name), field.name))

    if not projections:
        # `SELECT COUNT(*) FROM orders` names the model but none of its fields.
        # The query still needs one row per anchor row, so project the key and
        # emit no joins at all.
        projections = [
            _aliased(_column(_BASE_ALIAS, column), column)
            for column in (base.primary_key or (base.column_names[0],))
        ]

    select = exp.select(*projections).from_(
        exp.alias_(exp.to_table(base.name), _BASE_ALIAS, table=True)
    )

    for _, path in sorted(join_paths.items()):
        select = select.join(
            _many_to_one_join(path),
            on=_join_condition(path),
            join_type="LEFT",
        )

    for child, step in sorted(child_steps.items()):
        subquery = _aggregate_subquery(child, step, child_fields[child], graph)
        alias = _aggregate_alias(child)
        select = select.join(
            exp.alias_(subquery.subquery(), alias, table=True),
            on=_aggregate_condition(step, alias),
            join_type="LEFT",
        )

    return select, len(join_paths) + len(child_steps)


def _many_to_one_join(path: tuple[JoinStep, ...]) -> exp.Expression:
    step = path[-1]
    return exp.alias_(exp.to_table(step.to_table), _path_alias(path), table=True)


def _join_condition(path: tuple[JoinStep, ...]) -> exp.Expression:
    step = path[-1]
    left_alias = _BASE_ALIAS if len(path) == 1 else _path_alias(path[:-1])
    right_alias = _path_alias(path)

    if len(step.from_columns) != len(step.to_columns):
        raise ExpansionError(
            f"foreign key {step.from_table} -> {step.to_table} has mismatched "
            f"column counts ({len(step.from_columns)} vs {len(step.to_columns)})"
        )

    return _conjunction(
        [
            exp.EQ(
                this=_column(left_alias, left),
                expression=_column(right_alias, right),
            )
            for left, right in zip(step.from_columns, step.to_columns, strict=True)
        ]
    )


def _aggregate_subquery(
    child: str, step: JoinStep, fields: list[LogicalField], graph: SchemaGraph
) -> exp.Select:
    """Group a child table by its foreign key before it is ever joined.

    This is the grain guard. The subquery yields at most one row per parent key,
    so the join that follows cannot change the parent's row count.
    """
    group_columns = [exp.column(c) for c in step.to_columns]
    projections: list[exp.Expression] = list(group_columns)

    for field in fields:
        source = field.source
        if source.aggregate == "count":
            call: exp.Expression = exp.Count(this=exp.Star())
        elif source.aggregate == "sum":
            call = exp.Sum(this=exp.column(source.column))
        elif source.aggregate == "avg":
            call = exp.Avg(this=exp.column(source.column))
        elif source.aggregate == "max":
            call = exp.Max(this=exp.column(source.column))
        elif source.aggregate == "min":
            call = exp.Min(this=exp.column(source.column))
        else:
            raise ExpansionError(
                f"unsupported aggregate '{source.aggregate}' on field '{field.name}'"
            )
        projections.append(_aliased(call, field.name))

    if graph.schema.table(child) is None:
        raise ExpansionError(f"child table '{child}' is not in the schema")

    return exp.select(*projections).from_(exp.to_table(child)).group_by(*group_columns)


def _aggregate_condition(step: JoinStep, alias: str) -> exp.Expression:
    return _conjunction(
        [
            exp.EQ(
                this=_column(_BASE_ALIAS, left),
                expression=_column(alias, right),
            )
            for left, right in zip(step.from_columns, step.to_columns, strict=True)
        ]
    )


def _available_join_count(model: LogicalModel) -> int:
    paths: set[tuple[str, ...]] = set()
    children: set[str] = set()
    for field in model.fields:
        source = field.source
        if source.kind is FieldKind.JOINED:
            for depth in range(1, len(source.path) + 1):
                paths.add(_path_key(source.path[:depth]))
        elif source.kind is FieldKind.AGGREGATED and source.path:
            children.add(source.path[0].to_table)
    return len(paths) + len(children)


# ── naming and small builders ─────────────────────────────────────────────────


def _path_key(path: tuple[JoinStep, ...]) -> tuple[str, ...]:
    return tuple(step.to_table.lower() for step in path)


def _path_alias(path: tuple[JoinStep, ...]) -> str:
    """A stable alias per join path.

    Keyed on the whole path, not the target table: ``countries`` reached via
    ``addresses`` and via ``stores`` are two different joins and must not
    collapse onto one alias.
    """
    return "j_" + "__".join(step.to_table.lower() for step in path)


def _aggregate_alias(child: str) -> str:
    return f"agg_{child.lower()}"


def _column(table_alias: str, column: str) -> exp.Column:
    return exp.column(column, table=table_alias)


def _aliased(node: exp.Expression, alias: str) -> exp.Expression:
    return exp.alias_(node, alias)


def _conjunction(parts: list[exp.Expression]) -> exp.Expression:
    if not parts:
        raise ExpansionError("join has no key columns")
    condition = parts[0]
    for part in parts[1:]:
        condition = exp.And(this=condition, expression=part)
    return condition
