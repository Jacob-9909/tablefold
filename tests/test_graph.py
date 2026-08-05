from __future__ import annotations

from dataclasses import replace

import pytest
from tablefold.ir import Cardinality, name_aliases, singular

from tablefold.graph import infer_foreign_keys


def test_degrees_follow_key_direction(tiny_graph):
    # orders holds one FK and is pointed at by one.
    assert tiny_graph.out_degree("orders") == 1
    assert tiny_graph.in_degree("orders") == 1
    assert tiny_graph.out_degree("tiers") == 0
    assert tiny_graph.in_degree("tiers") == 1


def test_many_to_one_walk_chains_through_hops(tiny_graph):
    reached = dict(tiny_graph.walk_many_to_one("orders", max_hops=2))

    assert set(reached) == {"customers", "tiers"}
    assert len(reached["customers"]) == 1
    assert len(reached["tiers"]) == 2
    assert all(step.cardinality is Cardinality.MANY_TO_ONE for step in reached["tiers"])


def test_many_to_one_walk_respects_the_hop_budget(tiny_graph):
    reached = dict(tiny_graph.walk_many_to_one("orders", max_hops=1))

    assert set(reached) == {"customers"}


def test_walk_never_follows_keys_backwards(tiny_graph):
    # order_items points at orders; a forward walk from orders must not reach it,
    # because doing so would fan out and break the model's grain.
    reached = dict(tiny_graph.walk_many_to_one("orders", max_hops=3))

    assert "order_items" not in reached


def test_children_are_one_to_many(tiny_graph):
    children = dict(tiny_graph.children("orders"))

    assert set(children) == {"order_items"}
    step = children["order_items"]
    assert step.cardinality is Cardinality.ONE_TO_MANY
    assert step.from_columns == ("id",)
    assert step.to_columns == ("order_id",)


# ── inference ─────────────────────────────────────────────────────────────────


def test_inference_recovers_keys_stripped_from_a_dump(retail_schema):
    stripped = replace(retail_schema, foreign_keys=())
    recovered = infer_foreign_keys(stripped)

    edges = {(fk.from_table, fk.from_columns[0], fk.to_table) for fk in recovered}
    assert ("orders", "customer_id", "customers") in edges
    assert ("order_items", "order_id", "orders") in edges
    assert ("products", "category_id", "categories") in edges
    assert all(fk.inferred for fk in recovered)

    # Enough of the real graph comes back that the schema is foldable again.
    declared = {
        (fk.from_table.lower(), fk.from_columns[0].lower())
        for fk in retail_schema.foreign_keys
    }
    found = {(fk.from_table.lower(), fk.from_columns[0].lower()) for fk in recovered}
    assert len(found & declared) / len(declared) > 0.75


def test_inference_does_not_duplicate_declared_keys(retail_schema):
    assert infer_foreign_keys(retail_schema) == ()


def test_inference_skips_ambiguous_column_names(retail_schema):
    stripped = replace(retail_schema, foreign_keys=())
    recovered = infer_foreign_keys(stripped)

    # `audit_events.actor_id` names no table in the schema, and `categories.
    # parent_id` is a self-reference whose name gives no target away. Guessing
    # at either would wire up an edge that does not exist.
    assert not any(fk.from_columns == ("actor_id",) for fk in recovered)
    assert not any(fk.from_columns == ("parent_id",) for fk in recovered)


def test_inference_requires_a_compatible_type(tiny_schema):
    from tablefold.ir import PhysicalColumn, PhysicalTable

    # `tier_id` here is text while `tiers.id` is bigint, so the names line up
    # but the types cannot describe the same key.
    orders = PhysicalTable(
        name="orders",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("tier_id", "varchar(10)"),
        ),
        primary_key=("id",),
    )
    tiers = tiny_schema.table("tiers")
    schema = replace(tiny_schema, tables=(orders, tiers), foreign_keys=())

    assert infer_foreign_keys(schema) == ()


# ── naming ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("plural_name", "expected"),
    [
        ("orders", "order"),
        ("countries", "country"),
        ("companies", "company"),
        ("addresses", "address"),
        ("batches", "batch"),
        ("boxes", "box"),
        ("statuses", "status"),
        ("tax_rates", "tax_rate"),
        # Not plurals. `singular` used to strip these to `statu` / `addres`,
        # and the two copies of this logic disagreed about which.
        ("status", "status"),
        ("address", "address"),
        ("analysis", "analysis"),
    ],
)
def test_singular_handles_the_endings_that_used_to_diverge(plural_name, expected):
    assert singular(plural_name) == expected


def test_inference_and_field_prefixes_agree_on_one_spelling(retail_schema):
    """The two callers must strip a table name the same way.

    They were separate helpers with different rules, so `addresses` reached
    foreign-key inference as `address` and field naming as `addresse`.
    """
    for table in retail_schema.tables:
        assert singular(table.name) in name_aliases(table.name)
