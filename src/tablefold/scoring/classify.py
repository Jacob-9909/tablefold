"""Score every table on how fact-like it is.

The fold needs to pick a small number of *anchors* — tables whose grain a wide
model can sensibly sit at. Those are fact tables: rows describing events, with
measures to aggregate and a time at which they happened. Dimensions describe
things rather than events and make poor anchors, because a model anchored on a
dimension has nothing to measure.

Scoring is structural on purpose. No table names, no column-name vocabulary, no
model in the loop — the same schema always yields the same profile, and when a
result looks wrong the inputs are four numbers you can read off the table.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from tablefold.graph.graph import SchemaGraph
from tablefold.schema.ir import PhysicalTable

# Weights sum to 1.0. Measure density dominates because it is the signal that
# actually separates events from entities; the rest break ties.
_W_MEASURE = 0.40
_W_TEMPORAL = 0.20
_W_OUT_DEGREE = 0.25
_W_SIZE = 0.15

# A table scoring at or above this is treated as a fact.
FACT_THRESHOLD = 0.35

# Number of measure columns at which a table counts as fully measured. Below it,
# measure density is scaled down proportionally.
_MEASURE_SATURATION = 4


class TableRole(StrEnum):
    FACT = "fact"
    DIMENSION = "dimension"
    ISOLATED = "isolated"


@dataclass(frozen=True)
class TableProfile:
    name: str
    role: TableRole
    score: float
    measure_density: float
    temporal_count: int
    out_degree: int
    in_degree: int
    column_count: int

    @property
    def is_fact(self) -> bool:
        return self.role is TableRole.FACT


def profile_tables(graph: SchemaGraph) -> tuple[TableProfile, ...]:
    """Profile every table in the schema, highest-scoring first."""
    tables = graph.schema.tables
    if not tables:
        return ()

    max_out = max((graph.out_degree(t.name) for t in tables), default=0)
    row_counts = [t.row_estimate for t in tables if t.row_estimate]
    max_log_rows = math.log10(max(row_counts) + 1) if row_counts else 0.0

    profiles = [
        _profile_one(table, graph, max_out=max_out, max_log_rows=max_log_rows)
        for table in tables
    ]
    return tuple(sorted(profiles, key=lambda p: (-p.score, p.name)))


def _profile_one(
    table: PhysicalTable,
    graph: SchemaGraph,
    *,
    max_out: int,
    max_log_rows: float,
) -> TableProfile:
    out_degree = graph.out_degree(table.name)
    in_degree = graph.in_degree(table.name)

    key_columns = _key_columns(table, graph)
    measurable = [c for c in table.columns if c.name.lower() not in key_columns]

    numeric = [c for c in measurable if c.is_numeric]
    temporal = [c for c in table.columns if c.is_temporal]

    # Ratio alone is fooled by thin junction tables: ``cart_items(cart_id,
    # product_id, quantity)`` has one non-key column and it is numeric, giving a
    # perfect 1.0 density for a table with nothing to measure. Damping by the
    # absolute count separates "all measure" from "one measure".
    density = len(numeric) / len(measurable) if measurable else 0.0
    saturation = min(len(numeric), _MEASURE_SATURATION) / _MEASURE_SATURATION
    measure_density = density * saturation

    temporal_signal = min(len(temporal), 2) / 2

    out_signal = out_degree / max_out if max_out else 0.0

    if table.row_estimate and max_log_rows:
        size_signal = math.log10(table.row_estimate + 1) / max_log_rows
    else:
        # No statistics available. Stay neutral rather than penalising a table
        # for a fact the introspector could not observe.
        size_signal = 0.5

    score = (
        _W_MEASURE * measure_density
        + _W_TEMPORAL * temporal_signal
        + _W_OUT_DEGREE * out_signal
        + _W_SIZE * size_signal
    )

    if out_degree == 0 and in_degree == 0:
        role = TableRole.ISOLATED
    elif score >= FACT_THRESHOLD and out_degree > 0:
        role = TableRole.FACT
    else:
        role = TableRole.DIMENSION

    return TableProfile(
        name=table.name,
        role=role,
        score=round(score, 4),
        measure_density=round(measure_density, 4),
        temporal_count=len(temporal),
        out_degree=out_degree,
        in_degree=in_degree,
        column_count=len(table.columns),
    )


def _key_columns(table: PhysicalTable, graph: SchemaGraph) -> set[str]:
    """Primary-key and foreign-key columns.

    Excluded from measure density because a ``bigint`` FK is structurally
    numeric but carries no measurement — counting it would make every junction
    table look like a fact.
    """
    keys = {c.lower() for c in table.primary_key}
    for fk in graph.outgoing(table.name):
        keys.update(c.lower() for c in fk.from_columns)
    return keys
