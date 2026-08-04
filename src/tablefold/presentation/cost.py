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

# Aggregates emitted per numeric child column, in order of usefulness.
NUMERIC_AGGREGATES = ("sum", "avg")

# Cap on numeric columns aggregated from any single child, so one wide child
# table cannot crowd out every other field in the model.
MAX_AGGREGATED_COLUMNS_PER_CHILD = 3

# Field cap a model is composed under. Selection prices against the same cap,
# because a candidate whose fields would be trimmed does not actually cost what
# its raw column count suggests.
DEFAULT_MAX_FIELDS = 64


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
    max_fields: int = DEFAULT_MAX_FIELDS,
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
            # One COUNT, plus SUM and AVG over each aggregatable column.
            total += 1 + len(aggregatable_columns(child_table, graph)) * len(
                NUMERIC_AGGREGATES
            )

    return min(total, max_fields)
