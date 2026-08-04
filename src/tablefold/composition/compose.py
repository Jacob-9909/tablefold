"""Build one wide logical model per anchor.

A model has exactly one row per anchor row. Three kinds of field can live at
that grain, and the distinction is a correctness constraint rather than a
taxonomy:

* **base** — the anchor's own columns.
* **joined** — a column reached by following foreign keys *forwards*. At most
  one row matches, so inlining it cannot change the row count.
* **aggregated** — a column of a child table, which fans out. It can only
  appear folded through an aggregate. Inlining one would multiply the anchor's
  rows and silently corrupt every sum in the query.

Field count is capped. The point of the fold is a model an LLM reads in one
shot; a 400-field model would defeat it just as thoroughly as the 50 raw tables
did. When the cap binds, fields are dropped in reverse priority order — distant
joins go first, the anchor's own columns never go.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.cluster import Clustering
from tablefold.graph import SchemaGraph
from tablefold.ir import (
    FieldKind,
    FieldSource,
    JoinStep,
    LogicalField,
    LogicalLayer,
    LogicalModel,
    PhysicalTable,
)

# Columns that carry no business meaning at any grain.
_NOISE_SUFFIXES = ("_hash", "_token", "_secret", "_password", "_salt")

# Aggregates emitted per numeric child column, in order of usefulness.
_NUMERIC_AGGREGATES = ("sum", "avg")

# Cap on numeric columns aggregated from any single child, so one wide child
# table cannot crowd out every other field in the model.
_MAX_AGGREGATED_COLUMNS_PER_CHILD = 3


@dataclass(frozen=True)
class ComposeOptions:
    max_hops: int = 3
    max_fields: int = 64
    include_foreign_key_columns: bool = False
    include_aggregates: bool = True


def compose(
    graph: SchemaGraph,
    clustering: Clustering,
    *,
    options: ComposeOptions | None = None,
) -> LogicalLayer:
    """Turn each subject area into one wide logical model."""
    opts = options or ComposeOptions()
    models = tuple(
        _compose_one(graph, area.anchor, opts)
        for area in clustering.areas
        if graph.schema.table(area.anchor) is not None
    )
    notes = tuple(f"uncovered: {name}" for name in clustering.unassigned)
    return LogicalLayer(
        models=models,
        source_table_count=len(graph.schema.tables),
        notes=notes,
    )


def _compose_one(graph: SchemaGraph, anchor: str, opts: ComposeOptions) -> LogicalModel:
    table = graph.schema.table(anchor)
    assert table is not None  # guarded by the caller

    # (priority, field) — lower priority survives the cap.
    candidates: list[tuple[int, LogicalField]] = []
    absorbed: set[str] = {table.name}

    candidates.extend((0, f) for f in _base_fields(table, graph, opts))

    for target, path in graph.walk_many_to_one(anchor, max_hops=opts.max_hops):
        target_table = graph.schema.table(target)
        if target_table is None:
            continue
        absorbed.add(target_table.name)
        # One hop is a direct attribute of the anchor and ranks just after its
        # own columns; each further hop is progressively more incidental.
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
            # Aggregates outrank two-hop joins: a count of a model's children is
            # more often the answer than a distant lookup label.
            candidates.extend(
                (2, f) for f in _aggregated_fields(child_table, step, graph)
            )

    fields = _apply_budget(candidates, opts.max_fields)

    return LogicalModel(
        name=table.name,
        base_table=table.name,
        fields=fields,
        absorbed_tables=tuple(sorted(absorbed - {table.name})),
        description=(
            f"One row per {_singular(table.name)}. "
            f"Folds {len(absorbed)} physical tables."
        ),
    )


# ── field builders ────────────────────────────────────────────────────────────


def _base_fields(
    table: PhysicalTable, graph: SchemaGraph, opts: ComposeOptions
) -> list[LogicalField]:
    fk_columns = _fk_columns(table, graph)
    fields: list[LogicalField] = []

    for column in table.columns:
        lowered = column.name.lower()
        if _is_noise(lowered):
            continue
        if lowered in fk_columns and not opts.include_foreign_key_columns:
            # The referenced row's attributes are promoted in its place, so the
            # raw key would be a redundant integer in an LLM's context window.
            continue
        fields.append(
            LogicalField(
                name=column.name,
                type=column.type,
                source=FieldSource(
                    kind=FieldKind.BASE, table=table.name, column=column.name
                ),
                description=column.comment,
            )
        )
    return fields


def _joined_fields(
    table: PhysicalTable,
    path: tuple[JoinStep, ...],
    graph: SchemaGraph,
    opts: ComposeOptions,
) -> list[LogicalField]:
    """Promote a referenced table's descriptive columns onto the anchor."""
    fk_columns = _fk_columns(table, graph)
    pk_columns = {c.lower() for c in table.primary_key}
    prefix = _singular(table.name)

    fields: list[LogicalField] = []
    for column in table.columns:
        lowered = column.name.lower()
        if _is_noise(lowered) or lowered in pk_columns or lowered in fk_columns:
            continue
        fields.append(
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
        )
    return fields


def _aggregated_fields(
    table: PhysicalTable, step: JoinStep, graph: SchemaGraph
) -> list[LogicalField]:
    """Collapse a child table into measures at the parent's grain."""
    prefix = _plural(table.name)

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

    fk_columns = _fk_columns(table, graph)
    pk_columns = {c.lower() for c in table.primary_key}
    numeric = [
        c
        for c in table.columns
        if c.is_numeric
        and c.name.lower() not in fk_columns
        and c.name.lower() not in pk_columns
    ][:_MAX_AGGREGATED_COLUMNS_PER_CHILD]

    for column in numeric:
        for aggregate in _NUMERIC_AGGREGATES:
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


def _apply_budget(
    candidates: list[tuple[int, LogicalField]], max_fields: int
) -> tuple[LogicalField, ...]:
    """Deduplicate names, then keep the highest-priority ``max_fields``."""
    ordered = sorted(candidates, key=lambda pair: pair[0])

    kept: list[LogicalField] = []
    seen: set[str] = set()

    for _, field in ordered:
        name = field.name
        if name.lower() in seen:
            name = _disambiguate(field)
            if name.lower() in seen:
                continue
            field = LogicalField(
                name=name,
                type=field.type,
                source=field.source,
                description=field.description,
            )
        seen.add(name.lower())
        kept.append(field)
        if len(kept) >= max_fields:
            break

    return tuple(kept)


def _disambiguate(field: LogicalField) -> str:
    """Break a name collision using the field's own join path.

    ``countries.name`` reached through ``addresses`` and through ``stores`` are
    different columns with the same promoted name; the path is the only thing
    that distinguishes them, so it goes in the name.
    """
    source = field.source
    if not source.path:
        return f"{_singular(source.table)}_{source.column}"
    via = _singular(source.path[-1].from_table)
    return f"{via}_{field.name}"


def _fk_columns(table: PhysicalTable, graph: SchemaGraph) -> set[str]:
    return {c.lower() for fk in graph.outgoing(table.name) for c in fk.from_columns}


def _is_noise(column_name: str) -> bool:
    return any(column_name.endswith(suffix) for suffix in _NOISE_SUFFIXES)


def _prefixed(prefix: str, column: str) -> str:
    """Prefix a promoted column, unless it already reads that way.

    ``customers.customer_id`` promoted onto orders should stay
    ``customer_id``, not become ``customer_customer_id``.
    """
    lowered = column.lower()
    if lowered.startswith(f"{prefix}_") or lowered == prefix:
        return column
    return f"{prefix}_{column}"


def _singular(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith("ies"):
        return lowered[:-3] + "y"
    if lowered.endswith("sses") or lowered.endswith("ches"):
        return lowered[:-2]
    if lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


def _plural(name: str) -> str:
    lowered = name.lower()
    return lowered if lowered.endswith("s") else f"{lowered}s"
