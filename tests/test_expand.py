from __future__ import annotations

import sqlite3

import pytest

from tablefold.classify import profile_tables
from tablefold.cluster import SelectionPolicy, cluster
from tablefold.compose import compose
from tablefold.expand import ExpansionError, expand


@pytest.fixture
def tiny_layer(tiny_graph):
    clustering = cluster(
        tiny_graph, profile_tables(tiny_graph), policy=SelectionPolicy(max_areas=2)
    )
    return compose(tiny_graph, clustering)


@pytest.fixture
def live_db():
    """The tiny schema, populated so fan-out is observable.

    Order 1 has two items and order 2 has one. Any expansion that joins
    ``order_items`` without grouping it first will count order 1 twice.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tiers (id INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, tier_id INTEGER, email TEXT, name TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, customer_id INTEGER,
            total NUMERIC, tax NUMERIC, placed_at TEXT
        );
        CREATE TABLE order_items (
            id INTEGER PRIMARY KEY, order_id INTEGER,
            quantity INTEGER, line_total NUMERIC
        );

        INSERT INTO tiers VALUES (1, 'gold'), (2, 'silver');
        INSERT INTO customers VALUES (1, 1, 'a@example.com', 'Ada'),
                                     (2, 2, 'b@example.com', 'Bo');
        INSERT INTO orders VALUES (1, 1, 100, 10, '2026-01-05'),
                                  (2, 2, 50, 5, '2026-02-05');
        INSERT INTO order_items VALUES (1, 1, 2, 60), (2, 1, 1, 40), (3, 2, 5, 50);
        """
    )
    yield conn
    conn.close()


def _run(conn, layer, graph, sql):
    expansion = expand(sql, layer, graph, dialect="sqlite", pretty=False)
    return conn.execute(expansion.sql).fetchall(), expansion


# ── grain safety ──────────────────────────────────────────────────────────────


def test_a_child_table_does_not_inflate_the_parents_measures(
    live_db, tiny_layer, tiny_graph
):
    """The failure this whole design exists to prevent.

    Order 1 has two line items. Joining ``order_items`` directly would repeat
    its ``total`` of 100 twice and report revenue of 250 instead of 150 —
    with no error, in a query that looks correct.
    """
    rows, _ = _run(
        live_db,
        tiny_layer,
        tiny_graph,
        "SELECT SUM(total) AS revenue FROM orders",
    )

    assert rows == [(150,)]


def test_aggregated_child_measures_are_correct(live_db, tiny_layer, tiny_graph):
    rows, _ = _run(
        live_db,
        tiny_layer,
        tiny_graph,
        "SELECT SUM(order_items_line_total_sum) AS lines FROM orders",
    )

    assert rows == [(150,)]


def test_child_counts_are_per_parent_row(live_db, tiny_layer, tiny_graph):
    rows, _ = _run(
        live_db,
        tiny_layer,
        tiny_graph,
        "SELECT id, order_items_count FROM orders ORDER BY id",
    )

    assert rows == [(1, 2), (2, 1)]


def test_row_count_matches_the_anchor(live_db, tiny_layer, tiny_graph):
    rows, _ = _run(live_db, tiny_layer, tiny_graph, "SELECT COUNT(*) AS n FROM orders")

    assert rows == [(2,)]


def test_two_hop_joins_resolve_to_the_right_row(live_db, tiny_layer, tiny_graph):
    rows, _ = _run(
        live_db,
        tiny_layer,
        tiny_graph,
        "SELECT tier_label, SUM(total) AS revenue FROM orders "
        "GROUP BY tier_label ORDER BY tier_label",
    )

    assert rows == [("gold", 100), ("silver", 50)]


def test_a_parent_with_no_children_is_not_dropped(live_db, tiny_layer, tiny_graph):
    live_db.execute("INSERT INTO orders VALUES (3, 1, 25, 2, '2026-03-05')")

    rows, _ = _run(
        live_db,
        tiny_layer,
        tiny_graph,
        "SELECT id, order_items_count FROM orders ORDER BY id",
    )

    # The aggregate join must be a LEFT join; an inner one would silently drop
    # every order that has no line items yet.
    assert [r[0] for r in rows] == [1, 2, 3]
    assert rows[2][1] is None


def test_filters_and_ordering_survive_the_rewrite(live_db, tiny_layer, tiny_graph):
    rows, _ = _run(
        live_db,
        tiny_layer,
        tiny_graph,
        "SELECT id FROM orders WHERE placed_at >= '2026-02-01' ORDER BY id DESC",
    )

    assert rows == [(2,)]


# ── pruning ───────────────────────────────────────────────────────────────────


def test_only_the_joins_a_query_needs_are_emitted(tiny_layer, tiny_graph):
    expansion = expand("SELECT total FROM orders", tiny_layer, tiny_graph)

    assert expansion.joins_emitted == 0
    assert expansion.joins_available > 0
    assert expansion.joins_pruned == expansion.joins_available
    assert "customers" not in expansion.sql


def test_a_two_hop_field_pulls_in_its_intermediate_join(tiny_layer, tiny_graph):
    expansion = expand("SELECT tier_label FROM orders", tiny_layer, tiny_graph)

    # `tiers` is only reachable through `customers`, so both must be joined.
    assert expansion.joins_emitted == 2
    assert "customers" in expansion.sql
    assert "tiers" in expansion.sql


def test_count_star_does_not_disable_pruning(tiny_layer, tiny_graph):
    """``COUNT(*)`` contains a star but is not ``SELECT *``.

    Treating it as one would silently expand every join in the model for the
    most common aggregate query there is.
    """
    expansion = expand("SELECT COUNT(*) AS n FROM orders", tiny_layer, tiny_graph)

    assert expansion.joins_emitted == 0


def test_select_star_expands_the_whole_model(tiny_layer, tiny_graph):
    expansion = expand("SELECT * FROM orders", tiny_layer, tiny_graph)

    assert expansion.joins_emitted == expansion.joins_available


# ── errors ────────────────────────────────────────────────────────────────────


def test_unknown_model_is_rejected(tiny_layer, tiny_graph):
    with pytest.raises(ExpansionError, match="no logical model"):
        expand("SELECT * FROM nowhere", tiny_layer, tiny_graph)


def test_unknown_field_is_rejected_with_a_hint(tiny_layer, tiny_graph):
    with pytest.raises(ExpansionError, match="available:"):
        expand("SELECT nonexistent_field FROM orders", tiny_layer, tiny_graph)


def test_unparseable_sql_is_rejected(tiny_layer, tiny_graph):
    with pytest.raises(ExpansionError, match="could not parse"):
        expand("SELECT FROM WHERE", tiny_layer, tiny_graph)


# ── the full schema ───────────────────────────────────────────────────────────


def test_every_model_field_expands(retail_graph):
    """Each field in every model must survive a round trip through expansion.

    A field that composes but cannot expand is worse than a missing one: it
    appears in the prompt, an LLM uses it, and the query fails at the database.
    """
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    layer = compose(retail_graph, clustering)

    for model in layer.models:
        for field in model.fields:
            expansion = expand(
                f'SELECT "{field.name}" FROM "{model.name}"', layer, retail_graph
            )
            assert field.name in expansion.sql
