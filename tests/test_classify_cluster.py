from __future__ import annotations

from dataclasses import replace

from tablefold.classify import TableRole, profile_tables
from tablefold.cluster import cluster, reachable_tables
from tablefold.graph import SchemaGraph
from tablefold.ir import PhysicalColumn, PhysicalTable


def _profile(graph, name):
    return next(p for p in profile_tables(graph) if p.name == name)


def test_measure_bearing_event_table_outranks_its_lookups(tiny_graph):
    profiles = {p.name: p for p in profile_tables(tiny_graph)}

    assert profiles["orders"].score > profiles["tiers"].score
    assert profiles["orders"].role is TableRole.FACT
    assert profiles["tiers"].role is TableRole.DIMENSION


def test_foreign_key_columns_do_not_count_as_measures(tiny_graph):
    # orders has customer_id (bigint) but it is a key, so only total and tax
    # count toward measure density.
    orders = _profile(tiny_graph, "orders")

    assert orders.measure_density > 0
    assert orders.measure_density < 1.0


def test_a_thin_junction_table_does_not_look_like_a_fact():
    """One numeric column at 100% density must not beat a real fact table.

    Without damping by absolute measure count, ``cart_items(cart_id,
    product_id, quantity)`` scores a perfect density of 1.0 and outranks an
    order table with five measures — which then anchors a model with nothing to
    measure.
    """
    junction = PhysicalTable(
        name="cart_items",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("cart_id", "bigint"),
            PhysicalColumn("product_id", "bigint"),
            PhysicalColumn("quantity", "integer"),
        ),
        primary_key=("id",),
    )
    carts = PhysicalTable(
        name="carts",
        columns=(PhysicalColumn("id", "bigint", nullable=False),),
        primary_key=("id",),
    )
    products = PhysicalTable(
        name="products",
        columns=(PhysicalColumn("id", "bigint", nullable=False),),
        primary_key=("id",),
    )
    from tablefold.ir import ForeignKey, PhysicalSchema

    schema = PhysicalSchema(
        tables=(junction, carts, products),
        foreign_keys=(
            ForeignKey("cart_items", ("cart_id",), "carts", ("id",)),
            ForeignKey("cart_items", ("product_id",), "products", ("id",)),
        ),
    )
    graph = SchemaGraph.build(schema)

    assert _profile(graph, "cart_items").measure_density < 0.5


def test_isolated_tables_are_marked_as_such(tiny_schema):
    orphan = PhysicalTable(
        name="audit", columns=(PhysicalColumn("id", "bigint"),), primary_key=("id",)
    )
    graph = SchemaGraph.build(
        replace(tiny_schema, tables=(*tiny_schema.tables, orphan))
    )

    assert _profile(graph, "audit").role is TableRole.ISOLATED


# ── clustering ────────────────────────────────────────────────────────────────


def test_reach_covers_forward_joins_and_direct_children(tiny_graph):
    reach = reachable_tables(tiny_graph, "orders", max_hops=2)

    assert reach == {"orders", "customers", "tiers", "order_items"}


def test_reach_excludes_grandchildren(tiny_graph):
    # Nothing at order grain can expose a column of a child's child without
    # aggregating an aggregate, so claiming coverage of one would be a lie.
    reach = reachable_tables(tiny_graph, "tiers", max_hops=3)

    assert "orders" not in reach
    assert reach == {"tiers", "customers"}


def test_anchors_spread_across_the_schema_instead_of_clustering(retail_graph):
    result = cluster(retail_graph, profile_tables(retail_graph), target_areas=4)

    anchors = {area.anchor for area in result.areas}
    assert anchors == {"orders", "products", "customers", "shipments"}


def test_more_anchors_never_lose_coverage(retail_graph):
    profiles = profile_tables(retail_graph)

    coverage = [
        cluster(retail_graph, profiles, target_areas=n).covered_table_count
        for n in (2, 3, 4, 5, 6)
    ]
    assert coverage == sorted(coverage)


def test_a_hub_dimension_can_anchor_a_model(retail_graph):
    """``customers`` has no measures, so fact scoring alone would exclude it.

    Eight tables reference it. Without hub anchoring, that whole half of the
    schema — contacts, consents, loyalty, sessions — is unreachable from any
    model.
    """
    profiles = profile_tables(retail_graph)
    customers = next(p for p in profiles if p.name == "customers")
    assert customers.role is TableRole.DIMENSION

    anchors = {a.anchor for a in cluster(retail_graph, profiles, target_areas=4).areas}
    assert "customers" in anchors


def test_target_is_a_ceiling_not_a_quota(tiny_graph):
    # Only one table in the tiny schema can absorb anything new.
    result = cluster(tiny_graph, profile_tables(tiny_graph), target_areas=10)

    assert len(result.areas) < 10


def test_areas_may_share_a_conformed_dimension(retail_graph):
    result = cluster(retail_graph, profile_tables(retail_graph), target_areas=4)
    membership = {area.anchor: set(area.members) for area in result.areas}

    # `countries` is referenced from addresses, stores, suppliers and
    # warehouses; a single-owner assignment would strip it from three models.
    owners = [
        anchor for anchor, members in membership.items() if "countries" in members
    ]
    assert len(owners) > 1


def test_empty_schema_folds_to_nothing():
    from tablefold.ir import PhysicalSchema

    graph = SchemaGraph.build(PhysicalSchema(tables=()))
    result = cluster(graph, profile_tables(graph))

    assert result.areas == ()
