"""The whole fold, in one call.

Kept separate from the CLI so the pipeline is usable from a notebook, a test,
or whatever text-to-SQL layer ends up sitting on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.classify import TableProfile, profile_tables
from tablefold.cluster import Clustering, cluster
from tablefold.compose import ComposeOptions, compose
from tablefold.graph import SchemaGraph, infer_foreign_keys
from tablefold.ir import LogicalLayer, PhysicalSchema


@dataclass(frozen=True)
class FoldResult:
    schema: PhysicalSchema
    graph: SchemaGraph
    profiles: tuple[TableProfile, ...]
    clustering: Clustering
    layer: LogicalLayer
    inferred_foreign_keys: int


def fold(
    schema: PhysicalSchema,
    *,
    target_models: int = 4,
    max_hops: int = 3,
    max_fields: int = 64,
    infer_missing_keys: bool = True,
    include_aggregates: bool = True,
) -> FoldResult:
    """Fold a physical schema into ``target_models`` wide logical models.

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
    clustering = cluster(graph, profiles, target_areas=target_models, max_hops=max_hops)
    layer = compose(
        graph,
        clustering,
        options=ComposeOptions(
            max_hops=max_hops,
            max_fields=max_fields,
            include_aggregates=include_aggregates,
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
