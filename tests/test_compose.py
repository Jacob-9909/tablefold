from __future__ import annotations

import pytest

from tablefold.classify import profile_tables
from tablefold.cluster import SelectionPolicy, cluster
from tablefold.compose import ComposeOptions, compose
from tablefold.ir import Cardinality, FieldKind


@pytest.fixture
def tiny_layer(tiny_graph):
    clustering = cluster(
        tiny_graph, profile_tables(tiny_graph), policy=SelectionPolicy(max_areas=2)
    )
    return compose(tiny_graph, clustering)


def test_model_is_anchored_on_its_base_table(tiny_layer):
    orders = tiny_layer.model("orders")

    assert orders is not None
    assert orders.base_table == "orders"


def test_base_columns_keep_their_names(tiny_layer):
    orders = tiny_layer.model("orders")

    assert orders.field("total").source.kind is FieldKind.BASE
    assert orders.field("placed_at").source.kind is FieldKind.BASE


def test_foreign_key_columns_are_replaced_by_what_they_point_at(tiny_layer):
    orders = tiny_layer.model("orders")

    # The raw key is dropped; the row it identifies is promoted instead.
    assert orders.field("customer_id") is None
    assert orders.field("customer_email") is not None


def test_promoted_columns_are_prefixed_by_their_source(tiny_layer):
    orders = tiny_layer.model("orders")
    email = orders.field("customer_email")

    assert email.source.kind is FieldKind.JOINED
    assert (email.source.table, email.source.column) == ("customers", "email")
    assert email.source.hops == 1


def test_two_hop_columns_carry_the_whole_path(tiny_layer):
    orders = tiny_layer.model("orders")
    label = orders.field("tier_label")

    assert label is not None
    assert label.source.hops == 2
    assert [s.to_table for s in label.source.path] == ["customers", "tiers"]
    assert all(s.cardinality is Cardinality.MANY_TO_ONE for s in label.source.path)


def test_child_columns_are_only_ever_aggregated(tiny_layer):
    """A one-to-many child must not contribute a plain column.

    Inlining one would multiply the anchor's rows, and every sum in a query
    over the model would come back inflated with no error raised.
    """
    orders = tiny_layer.model("orders")

    from_child = [f for f in orders.fields if f.source.table == "order_items"]
    assert from_child
    assert all(f.source.kind is FieldKind.AGGREGATED for f in from_child)
    assert all(f.source.aggregate for f in from_child)
    assert all(
        f.source.path[0].cardinality is Cardinality.ONE_TO_MANY for f in from_child
    )


def test_children_contribute_a_row_count(tiny_layer):
    count = tiny_layer.model("orders").field("order_items_count")

    assert count is not None
    assert count.source.aggregate == "count"
    assert count.source.column == "*"


def test_aggregates_can_be_switched_off(tiny_graph):
    clustering = cluster(
        tiny_graph, profile_tables(tiny_graph), policy=SelectionPolicy(max_areas=2)
    )
    layer = compose(
        tiny_graph, clustering, options=ComposeOptions(include_aggregates=False)
    )

    orders = layer.model("orders")
    assert all(f.source.kind is not FieldKind.AGGREGATED for f in orders.fields)


def test_field_names_are_unique_within_a_model(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering)

    for model in layer.models:
        names = [f.name.lower() for f in model.fields]
        assert len(names) == len(set(names)), model.name


def test_the_budget_is_spent_on_the_layer_not_per_model(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering, options=ComposeOptions(field_budget=60))

    # The budget binds on the sum, not on any single model.
    assert layer.field_count == 60

    # And it is shared: every model got to state its own columns before any
    # model spent budget on a distant join.
    for model in layer.models:
        assert model.fields, f"{model.name} was starved"


def test_base_columns_outrank_everything_under_a_tight_budget(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering, options=ComposeOptions(field_budget=40))

    orders = layer.model("orders")
    base_columns = {c.name for c in retail_graph.schema.table("orders").columns}
    kept_base = {f.name for f in orders.fields if f.source.kind is FieldKind.BASE}
    assert kept_base <= base_columns
    assert len(kept_base) >= 10


def test_no_single_model_may_exceed_its_ceiling(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(
        retail_graph,
        clustering,
        options=ComposeOptions(field_budget=10_000, max_model_fields=20),
    )

    assert all(len(m.fields) <= 20 for m in layer.models)


def test_hop_budget_bounds_how_far_fields_travel(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(
        retail_graph, clustering, options=ComposeOptions(max_hops=1, field_budget=200)
    )

    for model in layer.models:
        assert all(f.source.hops <= 1 for f in model.fields)


def test_the_fold_actually_compresses(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering)

    assert len(layer.models) == 4
    assert layer.source_table_count == 53
    assert layer.source_column_count == 282

    # Columns per field. Unlike tables-per-model this cannot be improved by
    # dropping tables — a discarded table's columns leave the numerator too.
    assert layer.compression_ratio > 1.5


def test_uncovered_tables_are_reported_not_hidden(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering)

    assert layer.notes
    assert all(note.startswith("uncovered: ") for note in layer.notes)
