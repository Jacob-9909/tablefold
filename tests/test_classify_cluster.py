from __future__ import annotations

from dataclasses import replace

import pytest

from tablefold.choose.classify import FACT_THRESHOLD, TableRole, profile_tables
from tablefold.choose.cluster import (
    SelectionPolicy,
    StopReason,
    cluster,
    reachable_tables,
)
from tablefold.ir import PhysicalColumn, PhysicalTable
from tablefold.relate.graph import SchemaGraph


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
    result = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )

    # `shipments` would be the fourth on gain alone, but it overlaps `orders` by
    # ten of its thirteen tables and charges 48 fields for the three that are
    # new — the price ceiling turns it down in favour of a cheaper reach.
    anchors = {area.anchor for area in result.areas}
    assert anchors == {"orders", "products", "customers", "purchase_orders"}


def test_more_anchors_never_lose_coverage(retail_graph):
    profiles = profile_tables(retail_graph)

    coverage = [
        cluster(
            retail_graph, profiles, policy=SelectionPolicy(max_areas=n)
        ).covered_table_count
        for n in (2, 3, 4, 5, 6)
    ]
    assert coverage == sorted(coverage)


def test_a_hub_dimension_can_anchor_a_model(retail_graph):
    """``customers`` has no measures, so fact scoring alone would exclude it.

    Eight tables reference it, and a customer-grained model absorbs all eight.
    Selection has to rank on coverage rather than on role, or that whole half of
    the schema — contacts, consents, loyalty, sessions — is unreachable.
    """
    profiles = profile_tables(retail_graph)
    customers = next(p for p in profiles if p.name == "customers")
    assert customers.role is TableRole.DIMENSION

    anchors = {
        a.anchor
        for a in cluster(
            retail_graph, profiles, policy=SelectionPolicy(max_areas=4)
        ).areas
    }
    assert "customers" in anchors


def test_a_low_scoring_table_anchors_when_nothing_else_reaches_it(retail_graph):
    """Anchor eligibility is coverage, not score.

    ``employees`` scores 0.24 and nothing references it, so a fact-or-hub
    candidate pool excluded it — and because it is a grandchild of every anchor
    that could see it, excluding it stranded four tables that no other anchor
    reaches.
    """
    profiles = profile_tables(retail_graph)
    employees = next(p for p in profiles if p.name == "employees")
    assert employees.score < FACT_THRESHOLD
    assert employees.in_degree == 0

    result = cluster(
        retail_graph,
        profiles,
        policy=SelectionPolicy(coverage_target=1.0, min_gain=1),
    )

    area = next(a for a in result.areas if a.anchor == "employees")
    assert set(area.members) >= {"employees", "stores"}


def test_max_areas_is_a_ceiling_not_a_quota(tiny_graph):
    # Only one table in the tiny schema can absorb anything new.
    result = cluster(
        tiny_graph, profile_tables(tiny_graph), policy=SelectionPolicy(max_areas=10)
    )

    assert len(result.areas) < 10


# ── selection policy ──────────────────────────────────────────────────────────


def test_model_count_is_an_output_not_an_input(retail_graph):
    """No fixed target: ask for full coverage and take however many that costs."""
    result = cluster(
        retail_graph,
        profile_tables(retail_graph),
        policy=SelectionPolicy(
            coverage_target=1.0, min_gain=1, max_fields_per_table=float("inf")
        ),
    )

    assert result.coverage == 1.0
    assert result.unassigned == ()
    assert result.stop_reason is StopReason.COVERAGE_REACHED
    # The count is whatever the objective needed — the point is that it is not 4.
    assert len(result.areas) > 4


def test_a_model_must_not_overcharge_for_the_tables_it_adds(retail_graph):
    """Gain alone makes a model look free. The reader pays for its field list.

    ``shipments`` reaches three tables nothing else covers and spends 48 fields
    doing it. Ranking on gain took that buy; the price ceiling declines it, and
    coverage that expensive is left to the caller to ask for explicitly.
    """
    profiles = profile_tables(retail_graph)

    priced = cluster(retail_graph, profiles, policy=SelectionPolicy())
    unpriced = cluster(
        retail_graph,
        profiles,
        policy=SelectionPolicy(max_fields_per_table=float("inf")),
    )

    assert "shipments" not in {a.anchor for a in priced.areas}
    assert "shipments" in {a.anchor for a in unpriced.areas}

    def fields(result):
        return sum(a.estimated_fields for a in result.areas)

    # Fewer models and fewer fields, for a coverage loss the report names.
    assert len(priced.areas) < len(unpriced.areas)
    assert fields(priced) < fields(unpriced)
    assert priced.covered_table_count < unpriced.covered_table_count


def test_every_area_is_priced_at_what_it_will_actually_cost(retail_graph):
    """Selection prices models it has not built. The estimate has to be the
    same rules ``compose`` builds from, or the objective optimises against a
    model nobody produces."""
    from tablefold.build.compose import compose

    result = cluster(retail_graph, profile_tables(retail_graph))
    layer = compose(retail_graph, result)

    for area in result.areas:
        model = layer.model(area.anchor)
        assert model is not None
        assert area.estimated_fields == len(model.fields)


def test_min_gain_trades_coverage_for_a_smaller_layer(retail_graph):
    """The knob that finds the knee: a model has to pay for its own field list."""
    profiles = profile_tables(retail_graph)

    cheap = cluster(retail_graph, profiles, policy=SelectionPolicy(min_gain=1))
    strict = cluster(retail_graph, profiles, policy=SelectionPolicy(min_gain=3))

    assert len(strict.areas) < len(cheap.areas)
    assert strict.covered_table_count < cheap.covered_table_count


def test_falling_short_of_the_target_is_reported_not_hidden(retail_graph):
    """A layer that could not reach its target says so, and names no more models.

    Silently returning 83% against a 95% request is the failure mode worth
    guarding: the caller would read the model list and assume it was complete.
    """
    result = cluster(
        retail_graph,
        profile_tables(retail_graph),
        policy=SelectionPolicy(coverage_target=0.95, min_gain=3),
    )

    assert result.coverage < 0.95
    assert result.stop_reason is StopReason.GAIN_EXHAUSTED
    assert result.unassigned


def test_hitting_the_ceiling_is_distinguishable_from_running_out(retail_graph):
    result = cluster(
        retail_graph,
        profile_tables(retail_graph),
        policy=SelectionPolicy(coverage_target=1.0, min_gain=1, max_areas=3),
    )

    assert len(result.areas) == 3
    assert result.stop_reason is StopReason.MAX_AREAS


def test_areas_record_what_they_were_the_first_to_cover(retail_graph):
    result = cluster(retail_graph, profile_tables(retail_graph))

    gains = [area.new_tables for area in result.areas]
    assert gains == sorted(gains, reverse=True)  # greedy takes the best gain first
    assert sum(gains) == result.covered_table_count


@pytest.mark.parametrize(
    "kwargs",
    [
        {"coverage_target": 1.5},
        {"coverage_target": -0.1},
        {"min_gain": 0},
        {"max_fields_per_table": 0},
        {"max_fields_per_table": -1.0},
        {"max_areas": 0},
    ],
)
def test_policy_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        SelectionPolicy(**kwargs)


def test_areas_may_share_a_conformed_dimension(retail_graph):
    result = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
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
