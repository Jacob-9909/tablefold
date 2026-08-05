"""The whole fold, in one call.

Kept separate from the CLI so the pipeline is usable from a notebook, a test,
or whatever text-to-SQL layer ends up sitting on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.build.compose import ComposeOptions, compose
from tablefold.choose.classify import TableProfile, profile_tables
from tablefold.choose.cluster import Clustering, cluster
from tablefold.choose.cost import DEFAULT_FIELD_BUDGET, MAX_MODEL_FIELDS
from tablefold.choose.select import SelectionPolicy, Selector
from tablefold.ir import LogicalLayer, PhysicalSchema
from tablefold.relate.graph import SchemaGraph, infer_foreign_keys


@dataclass(frozen=True)
class FoldResult:
    schema: PhysicalSchema
    graph: SchemaGraph
    profiles: tuple[TableProfile, ...]
    clustering: Clustering
    layer: LogicalLayer
    inferred_foreign_keys: int

    @property
    def tier1_covered_tables(self) -> set[str]:
        covered: set[str] = set()
        for model in self.layer.models:
            covered.add(model.base_table.lower())
            for t in model.absorbed_tables:
                covered.add(t.lower())
        return covered

    @property
    def tier2_edge_tables(self) -> tuple[object, ...]:
        covered = self.tier1_covered_tables
        return tuple(t for t in self.schema.tables if t.name.lower() not in covered)


def fold(
    schema: PhysicalSchema,
    *,
    policy: SelectionPolicy | None = None,
    selector: Selector | None = None,
    max_hops: int = 3,
    field_budget: int = DEFAULT_FIELD_BUDGET,
    max_model_fields: int = MAX_MODEL_FIELDS,
    infer_missing_keys: bool = True,
    include_aggregates: bool = True,
    prefix_joined_fields: bool = True,
    expose_child_filters: bool = False,
) -> FoldResult:
    """Fold a physical schema into as few wide logical models as ``policy`` allows.

    The model count is an output, not an input: :class:`SelectionPolicy` states
    how much coverage is wanted and what a model has to earn to justify itself,
    and the fold reports how many that took.

    ``selector`` decides which anchors those are — greedy set cover by default,
    or :class:`~tablefold.choose.select.LLMSelector` when the semantic calls a
    foreign-key graph cannot make are worth a completion.

    ``infer_missing_keys`` recovers references from naming convention. It is on
    by default because a schema with no declared foreign keys yields no graph,
    and therefore no fold at all — and stripped constraints are the normal state
    of a restored backup rather than an edge case.
    """
    inferred = infer_foreign_keys(schema) if infer_missing_keys else ()
    enriched = (
        schema.with_foreign_keys(schema.foreign_keys + inferred) if inferred else schema
    )

    graph = SchemaGraph.build(enriched)
    profiles = profile_tables(graph)
    clustering = cluster(
        graph,
        profiles,
        policy=policy,
        selector=selector,
        max_hops=max_hops,
        max_fields=max_model_fields,
        include_aggregates=include_aggregates,
        expose_child_filters=expose_child_filters,
    )
    layer = compose(
        graph,
        clustering,
        options=ComposeOptions(
            max_hops=max_hops,
            field_budget=field_budget,
            max_model_fields=max_model_fields,
            include_aggregates=include_aggregates,
            prefix_joined_fields=prefix_joined_fields,
            expose_child_filters=expose_child_filters,
        ),
    )

    return FoldResult(
        schema=enriched,
        graph=graph,
        profiles=profiles,
        clustering=clustering,
        layer=layer,
        inferred_foreign_keys=len(inferred),
    )
