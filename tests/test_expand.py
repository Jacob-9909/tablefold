from __future__ import annotations

import sqlite3

import pytest

from tablefold.build.compose import ComposeOptions, compose
from tablefold.choose.classify import profile_tables
from tablefold.choose.cluster import SelectionPolicy, cluster
from tablefold.ir import FieldKind
from tablefold.rewrite.expand import ExpansionError, expand


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


def test_an_unknown_field_mixed_with_known_ones_is_still_rejected(
    tiny_layer, tiny_graph
):
    """부분 매칭이 통과하면 안 된다.

    예전에는 아는 필드가 하나라도 있으면 모르는 이름이 섞여도 통과했다. 그러면
    그 이름은 CTE에 투영되지 않은 채 바깥 SELECT에 남고, 실패는 여기가 아니라
    데이터베이스에서 일어난다 — 이름을 지어낸 곳에서 멀리 떨어진 지점이다.
    """
    with pytest.raises(ExpansionError, match="unknown fields"):
        expand("SELECT id, nonexistent_field FROM orders", tiny_layer, tiny_graph)


def test_an_output_alias_is_not_mistaken_for_an_unknown_field(tiny_layer, tiny_graph):
    """질의가 스스로 만든 이름은 모델의 필드가 아니어도 정상이다."""
    result = expand(
        "SELECT SUM(total) AS revenue FROM orders ORDER BY revenue",
        tiny_layer,
        tiny_graph,
    )
    assert "revenue" in result.sql


def test_joined_fields_can_keep_their_original_names(retail_graph):
    """``prefix_joined_fields=False`` 는 원본 컬럼 이름을 그대로 쓴다.

    테이블 이름이 사람이 읽을 말이 아닌 웨어하우스 스키마에서 필요하다 —
    ``D_SA_ORG.HEAD_NM`` 이 ``d_sa_org_HEAD_NM`` 이 되면 그 스키마의 실제
    질의와 어긋난다.
    """
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=2)
    )
    layer = compose(
        retail_graph,
        clustering,
        options=ComposeOptions(prefix_joined_fields=False, max_hops=1),
    )

    joined = [
        f for m in layer.models for f in m.fields if f.source.kind is FieldKind.JOINED
    ]
    assert joined
    # 접두사가 붙지 않았으므로 이름이 원본 컬럼과 같거나, 충돌해서 구분된 것뿐이다.
    assert any(f.name == f.source.column for f in joined)


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


# ── 술어 밀어넣기 ─────────────────────────────────────────────────────────────


@pytest.fixture
def filterable_layer(retail_graph):
    """자식의 원본 컬럼을 필터 전용으로 노출한 레이어."""
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=2)
    )
    return compose(
        retail_graph, clustering, options=ComposeOptions(expose_child_filters=True)
    )


def test_a_filter_on_a_child_column_lands_inside_the_aggregate(
    filterable_layer, retail_graph
):
    """사전집계에 조건을 거는 유일한 방법.

    ``SUM(...)`` 은 이미 전체를 더한 뒤이므로 바깥에서 자식의 행을 고를 수 없다.
    조건이 GROUP BY 앞으로 들어가야 "이 조건에 맞는 행만" 더해진다.
    """
    expansion = expand(
        "SELECT id, order_items_quantity_sum FROM orders "
        "WHERE order_items_product_id = 7",
        filterable_layer,
        retail_graph,
    )

    body = expansion.sql.split("SELECT\n  id")[0]
    assert "product_id = 7" in body
    assert "GROUP BY" in body
    # 조건은 서브쿼리로 옮겨졌으므로 바깥 필드 이름으로는 남지 않는다.
    assert "order_items_product_id" not in expansion.sql


def test_a_filter_only_field_cannot_be_selected(filterable_layer, retail_graph):
    """값으로 꺼낼 수 없는 필드다. 자식 행마다 달라 앵커 한 행에 대응이 없다."""
    with pytest.raises(ExpansionError, match="filter-only"):
        expand(
            "SELECT order_items_product_id FROM orders",
            filterable_layer,
            retail_graph,
        )


def test_a_filter_only_field_inside_an_or_is_rejected(filterable_layer, retail_graph):
    """OR 는 쪼개면 뜻이 달라지므로 밀어넣지 않고, 남으면 거부한다."""
    with pytest.raises(ExpansionError, match="filter-only"):
        expand(
            "SELECT id FROM orders WHERE order_items_product_id = 7 OR id > 10",
            filterable_layer,
            retail_graph,
        )


def test_filters_are_off_by_default(tiny_layer):
    """기본값에서는 필터 전용 필드가 생기지 않는다 — 레이어가 부풀지 않도록."""
    assert not [f for m in tiny_layer.models for f in m.fields if f.filter_only]


def test_a_parenthesized_and_still_pushes_down(filterable_layer, retail_graph):
    """LLM 이 습관처럼 붙이는 괄호 하나가 pushdown 을 막아선 안 된다.

    ``WHERE (조건 AND 조건)`` 의 괄호는 뜻을 바꾸지 않는다. 괄호를 불투명하게
    다루던 시절에는 이 질의가 ``FilterOnlyMisuse`` 로 죽었고, 죽은 자리는
    유료 수리 라운드가 메웠다 — 구조적으로 불가능한 목표를 붙들고.
    """
    expansion = expand(
        "SELECT id, order_items_quantity_sum FROM orders "
        "WHERE (order_items_product_id = 7 AND id > 10)",
        filterable_layer,
        retail_graph,
    )

    assert "product_id = 7" in expansion.sql
    assert "GROUP BY" in expansion.sql


def test_a_parenthesized_or_is_still_rejected(filterable_layer, retail_graph):
    """괄호 풀기가 OR 까지 풀면 뜻이 달라진다. OR 덩어리는 그대로 남는다."""
    with pytest.raises(ExpansionError, match="filter-only"):
        expand(
            "SELECT id FROM orders WHERE (order_items_product_id = 7 OR id > 10)",
            filterable_layer,
            retail_graph,
        )
