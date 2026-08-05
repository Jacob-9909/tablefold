"""확장이 조용히 틀리지 않는지 못 박는다.

여기 있는 것들은 전부 한 번 실제로 일어났던 오답이다. 예외를 던지지 않고 실행되며
값만 틀리는 종류라 테스트 없이는 다시 들어와도 알 수 없다.
"""

from __future__ import annotations

import pytest
from tablefold.cluster import SelectionPolicy
from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable

from tablefold.expansion.expand import ExpansionError, expand
from tablefold.pipeline import fold


@pytest.fixture
def filtered_fold():
    """자식에 조건을 걸 만한 컬럼이 있는 스키마.

    ``tiny_schema`` 로는 안 된다. 그쪽 ``order_items`` 는 컬럼이 키 아니면 측정값
    뿐이라 필터 전용 필드가 하나도 나오지 않는다 — 필터 전용은 "키도 측정값도
    아닌 것"의 자리이므로, 기간 컬럼이 있는 자식이 필요하다.
    """
    orders = PhysicalTable(
        name="orders",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("placed_at", "timestamp"),
        ),
        primary_key=("id",),
    )
    lines = PhysicalTable(
        name="order_lines",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("order_id", "bigint"),
            PhysicalColumn("shipped_on", "date"),
            PhysicalColumn("amount", "numeric"),
        ),
        primary_key=("id",),
    )
    schema = PhysicalSchema(
        tables=(orders, lines),
        foreign_keys=(ForeignKey("order_lines", ("order_id",), "orders", ("id",)),),
    )
    from tablefold.clustering.select import ExplicitSelector

    # 앵커를 못 박는다. 탐욕 선택은 두 테이블짜리 스키마에서 자식 쪽을 고를 수도
    # 있는데, 그러면 검증하려는 사전집계 구조가 아예 안 생긴다.
    return fold(
        schema,
        infer_missing_keys=False,
        expose_child_filters=True,
        field_budget=10_000,
        selector=ExplicitSelector(("orders",)),
        policy=SelectionPolicy(max_areas=1),
    )


# ── 모델 경계 ─────────────────────────────────────────────────────────────────


def test_two_models_in_one_query_are_rejected(retail_schema):
    """두 모델을 조인하면 이 레이어가 없애려던 문제가 그대로 돌아온다.

    게다가 두 모델이 같은 필드 이름을 가지면 (retail 의 orders 와 products 는
    8개를 공유) 확장 결과의 바깥 SELECT 가 한정자 없이 모호해져 데이터베이스에서
    터진다. 그 전에 여기서 막는다.
    """
    result = fold(retail_schema, policy=SelectionPolicy(max_areas=4))
    a, b = result.layer.models[0].name, result.layer.models[1].name

    with pytest.raises(ExpansionError, match="one model per query"):
        expand(
            f"SELECT * FROM {a} x JOIN {b} y ON x.id = y.id",
            result.layer,
            result.graph,
        )


def test_one_model_may_appear_more_than_once(retail_schema):
    """같은 모델을 다시 읽는 것은 막지 않는다.

    막을 이유가 없다 — 모든 참조가 같은 CTE 를 가리키므로 어느 필드인지 모호할
    일이 없다. 테이블 *노드* 개수로 판정하던 동안은
    ``WHERE x > (SELECT AVG(x) FROM m)`` 같은 정상 질의가 막혔다.
    """
    result = fold(retail_schema, policy=SelectionPolicy(max_areas=4))
    orders = result.layer.model("orders")
    value = next(
        f.name for f in orders.fields if f.type.upper().startswith("DECIMAL")
    )

    sql = expand(
        f"SELECT {value} FROM orders "
        f"WHERE {value} > (SELECT AVG({value}) FROM orders)",
        result.layer,
        result.graph,
    ).sql

    assert "tf__orders" in sql


def test_a_cte_shadowing_a_physical_table_is_rejected(retail_schema):
    """``WITH products AS (...)`` 는 확장이 읽을 물리 테이블을 가린다.

    데이터베이스는 정상적으로 실행하고 엉뚱한 원본에서 뽑은 값을 돌려준다.
    """
    result = fold(retail_schema, policy=SelectionPolicy(max_areas=4))
    name = result.layer.models[0].name

    with pytest.raises(ExpansionError, match="shadow"):
        expand(
            f"WITH {name} AS (SELECT 1 AS id) SELECT id FROM {name}",
            result.layer,
            result.graph,
        )


# ── 스키마 한정자 ─────────────────────────────────────────────────────────────


def test_schema_qualifier_survives_expansion():
    """``sales.orders`` 가 ``orders`` 로 나가면 다른 스키마의 동명 테이블을 읽는다."""
    orders = PhysicalTable(
        name="orders",
        schema="sales",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("customer_id", "bigint"),
            PhysicalColumn("total", "numeric"),
        ),
        primary_key=("id",),
    )
    customers = PhysicalTable(
        name="customers",
        schema="sales",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("email", "varchar"),
        ),
        primary_key=("id",),
    )
    schema = PhysicalSchema(
        tables=(orders, customers),
        foreign_keys=(ForeignKey("orders", ("customer_id",), "customers", ("id",)),),
    )
    result = fold(schema, infer_missing_keys=False)

    sql = expand(
        "SELECT customer_email, total FROM orders", result.layer, result.graph
    ).sql

    assert "sales.orders" in sql
    assert "sales.customers" in sql


# ── 사전집계에 걸린 조건 ──────────────────────────────────────────────────────


def test_a_filtered_child_joins_inner_not_left(filtered_fold):
    """"7월 매출"을 물으면 7월에 매출이 없는 부모는 답에서 빠져야 한다.

    LEFT 로 두면 그런 부모가 NULL 을 달고 살아남는다 — NL2SQL 실측에서 13행이
    나왔고 그중 5행이 NULL 이었다. 정답은 8행이다.
    """
    orders = filtered_fold.layer.model("orders")
    flt = next(f for f in orders.fields if f.filter_only)
    agg = next(f for f in orders.fields if f.source.aggregate and not f.filter_only)

    sql = expand(
        f"SELECT {agg.name} FROM orders WHERE {flt.name} > '2020-01-01'",
        filtered_fold.layer,
        filtered_fold.graph,
    ).sql

    assert "INNER JOIN" in sql.upper()
    assert "LEFT JOIN" not in sql.upper()


def test_an_unfiltered_child_stays_left(filtered_fold):
    """자식이 없다는 사실 자체가 답인 질문("주문이 없는 고객")을 지우면 안 된다."""
    orders = filtered_fold.layer.model("orders")
    agg = next(f for f in orders.fields if f.source.aggregate and not f.filter_only)

    sql = expand(
        f"SELECT {agg.name} FROM orders", filtered_fold.layer, filtered_fold.graph
    ).sql

    assert "LEFT JOIN" in sql.upper()
    assert "INNER JOIN" not in sql.upper()


def test_an_ambiguous_filter_name_is_not_pushed_to_a_guess(tiny_graph):
    """이름 하나가 여러 모델의 필터 전용 필드일 때 아무 데나 밀어 넣지 않는다.

    딕셔너리로 덮어쓰던 시절에는 나중 모델이 이겨서, 사용자가 지목하지 않은
    모델의 서브쿼리로 조건이 조용히 옮겨갔다.
    """
    import sqlglot
    from tablefold.ir import (
        Cardinality,
        FieldKind,
        FieldSource,
        JoinStep,
        LogicalField,
        LogicalModel,
    )

    from tablefold.expansion.expand import _pushdown

    step = JoinStep("a", ("id",), "child", ("a_id",), Cardinality.ONE_TO_MANY)
    field = LogicalField(
        name="shared_when",
        type="date",
        source=FieldSource(FieldKind.AGGREGATED, "child", "when", (step,)),
        filter_only=True,
    )
    two = (
        LogicalModel(name="a", base_table="a", fields=(field,)),
        LogicalModel(name="b", base_table="b", fields=(field,)),
    )

    _, pushed = _pushdown(
        sqlglot.parse_one("SELECT 1 FROM a WHERE shared_when > '2020-01-01'"), two
    )

    assert pushed == {}


# ── 가격 산정과 커버리지 ──────────────────────────────────────────────────────


def test_pricing_matches_what_compose_actually_builds(retail_schema):
    """추정과 실제가 어긋나면 심사 규칙이 틀린 값으로 후보를 통과시킨다.

    필터 전용 필드를 세지 않던 동안 ``customers`` 는 27 로 값이 매겨졌고
    실제로는 64 개 필드를 냈다.
    """
    from tablefold.graph import SchemaGraph as SG
    from tablefold.presentation.cost import estimate_fields

    graph = SG.build(retail_schema)
    result = fold(
        retail_schema,
        infer_missing_keys=False,
        field_budget=10_000,
        max_hops=1,
        expose_child_filters=True,
    )

    for model in result.layer.models:
        estimated = estimate_fields(
            graph,
            model.base_table,
            max_hops=1,
            cap=10_000,
            expose_child_filters=True,
        )
        # 이름 중복 제거 전의 상한이므로 실제보다 작아서는 안 된다.
        assert estimated >= len(model.fields), model.base_table


def test_coverage_does_not_claim_tables_the_fold_left_out(retail_schema):
    """집계를 끄면 자식은 어떤 필드도 내지 않으므로 흡수됐다고 말할 수 없다.

    어긋나 있던 동안 retail 은 66%(35/53)를 보고했지만 모델이 실제로 안은
    테이블은 13개였다.
    """
    result = fold(
        retail_schema,
        infer_missing_keys=False,
        include_aggregates=False,
        policy=SelectionPolicy(max_areas=3),
    )
    absorbed = {
        t.lower()
        for m in result.layer.models
        for t in (m.base_table, *m.absorbed_tables)
    }

    assert result.layer.covered_table_count == len(absorbed)


# ── 이름 ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("orders", "order"),
        ("addresses", "address"),
        ("categories", "category"),
        ("status", "status"),
        ("analysis", "analysis"),
        ("series", "series"),
        ("species", "species"),
        ("F_SALES", "f_sales"),
        ("news", "news"),
    ],
)
def test_singular_leaves_unchanged_plurals_alone(word, expected):
    from tablefold.ir import singular

    assert singular(word) == expected


def test_fact_score_is_not_flattened_when_no_row_counts_exist(retail_graph):
    """통계가 없으면 크기 항은 모든 테이블에 같은 값을 더한다.

    상수를 더하는 것은 순위를 바꾸지 않으면서 가중치의 15%를 죽이고 임계값만
    위로 민다. 그 경우 항을 빼고 나머지를 다시 정규화해야 한다.
    """
    from tablefold.classify import (
        _W_MEASURE,
        _W_OUT_DEGREE,
        _W_TEMPORAL,
        profile_tables,
    )

    assert all(t.row_estimate is None for t in retail_graph.schema.tables)

    profiles = profile_tables(retail_graph)
    top = profiles[0]
    max_out = max(retail_graph.out_degree(t.name) for t in retail_graph.schema.tables)

    weighted = (
        _W_MEASURE * top.measure_density
        + _W_TEMPORAL * min(top.temporal_count, 2) / 2
        + _W_OUT_DEGREE * top.out_degree / max_out
    )
    expected = weighted / (_W_MEASURE + _W_TEMPORAL + _W_OUT_DEGREE)

    assert top.score == pytest.approx(expected, abs=1e-4)
    # 상수 항이 살아 있던 시절의 값보다 반드시 커야 한다 (그때는 weighted + 0.075).
    assert top.score > weighted + 0.075


# ── 같은 두 테이블 사이의 관계가 여러 개일 때 ────────────────────────────────
#
# 두 테이블이 하나의 관계로만 이어진다는 가정이 여러 곳에 박혀 있었다. 실무
# 스키마에서는 흔하지 않은 모양이 아니다 — 구매자/판매자, 청구지/배송지,
# 출발/도착. 가정이 깨지면 조용히 한쪽이 사라지거나 같은 값이 두 번 나온다.


def _two_paths_to_one_table():
    users = PhysicalTable(
        name="users",
        columns=(
            PhysicalColumn("user_id", "bigint", nullable=False),
            PhysicalColumn("username", "varchar"),
        ),
        primary_key=("user_id",),
    )
    orders = PhysicalTable(
        name="orders",
        columns=(
            PhysicalColumn("order_id", "bigint", nullable=False),
            PhysicalColumn("buyer_id", "bigint"),
            PhysicalColumn("seller_id", "bigint"),
            PhysicalColumn("amount", "numeric"),
        ),
        primary_key=("order_id",),
    )
    return PhysicalSchema(
        tables=(users, orders),
        foreign_keys=(
            ForeignKey("orders", ("buyer_id",), "users", ("user_id",)),
            ForeignKey("orders", ("seller_id",), "users", ("user_id",)),
        ),
    )


def test_a_second_fk_to_the_same_table_is_not_swallowed():
    """``walk_many_to_one`` 이 테이블 단위로 방문을 기록하던 동안, 먼저 도달한
    경로가 대상을 소진해 판매자 정보가 모델에서 통째로 사라졌다."""
    from tablefold.clustering.select import ExplicitSelector

    result = fold(
        _two_paths_to_one_table(),
        infer_missing_keys=False,
        selector=ExplicitSelector(("orders",)),
        policy=SelectionPolicy(max_areas=1),
    )
    names = set(result.layer.model("orders").field_names)

    assert {"buyer_username", "seller_username"} <= names


def test_each_path_joins_on_its_own_key():
    """별칭이 대상 테이블 이름만으로 만들어지던 동안 두 경로가 한 별칭으로
    뭉개져, 두 필드가 같은 조인에서 나왔다."""
    from tablefold.clustering.select import ExplicitSelector

    result = fold(
        _two_paths_to_one_table(),
        infer_missing_keys=False,
        selector=ExplicitSelector(("orders",)),
        policy=SelectionPolicy(max_areas=1),
    )
    sql = expand(
        "SELECT buyer_username, seller_username FROM orders",
        result.layer,
        result.graph,
    ).sql

    assert "base.buyer_id" in sql
    assert "base.seller_id" in sql
    assert sql.upper().count("LEFT JOIN") == 2


def test_two_child_fks_get_two_subqueries():
    """자식이 부모를 두 키로 참조할 때 하나의 서브쿼리로 뭉치면, 두 집계가
    같은 ``GROUP BY`` 에서 나와 같은 값이 두 번 출력된다 — 에러 없이."""
    from tablefold.clustering.select import ExplicitSelector

    orders = PhysicalTable(
        name="orders",
        columns=(
            PhysicalColumn("order_id", "bigint", nullable=False),
            PhysicalColumn("amount", "numeric"),
        ),
        primary_key=("order_id",),
    )
    links = PhysicalTable(
        name="order_links",
        columns=(
            PhysicalColumn("link_id", "bigint", nullable=False),
            PhysicalColumn("src_order_id", "bigint"),
            PhysicalColumn("dst_order_id", "bigint"),
        ),
        primary_key=("link_id",),
    )
    schema = PhysicalSchema(
        tables=(orders, links),
        foreign_keys=(
            ForeignKey("order_links", ("src_order_id",), "orders", ("order_id",)),
            ForeignKey("order_links", ("dst_order_id",), "orders", ("order_id",)),
        ),
    )
    result = fold(
        schema,
        infer_missing_keys=False,
        selector=ExplicitSelector(("orders",)),
        policy=SelectionPolicy(max_areas=1),
        field_budget=10_000,
    )
    counts = [f for f in result.layer.model("orders").field_names if "count" in f]

    assert len(counts) == 2

    sql = expand(
        f"SELECT {', '.join(counts)} FROM orders", result.layer, result.graph
    ).sql

    assert "src_order_id" in sql
    assert "dst_order_id" in sql
    assert sql.upper().count("GROUP BY") == 2


# ── 추론과 이름 ───────────────────────────────────────────────────────────────


def test_generic_primary_keys_do_not_become_a_clique():
    """``id`` 는 어느 테이블을 가리키는지 스스로 말하지 못한다.

    허용하면 ``id`` 를 쓰는 모든 테이블이 서로를 참조하는 완전 그래프가 된다 —
    테이블 3개에서 가짜 엣지 6개.
    """
    from tablefold.graph.from_keys import infer_from_primary_keys

    def table(name):
        return PhysicalTable(
            name=name,
            columns=(
                PhysicalColumn("id", "bigint", nullable=False),
                PhysicalColumn("label", "varchar"),
            ),
            primary_key=("id",),
        )

    schema = PhysicalSchema(
        tables=(table("customers"), table("products"), table("orders"))
    )

    assert infer_from_primary_keys(schema) == ()


def test_a_selector_cannot_hide_a_model_behind_a_duplicate_name():
    """이름이 겹치면 ``layer.model()`` 이 첫 번째만 돌려주고, 두 번째 모델은
    있다고 보고되면서 이름으로 닿을 수 없게 된다."""
    from tablefold.clustering.select import Choice, Selection, StopReason

    class SameName:
        def select(self, lattice, policy):
            return Selection(
                (Choice("orders", "sales"), Choice("invoices", "sales")),
                StopReason.SELECTOR_CHOSE,
                "custom",
            )

        @property
        def label(self):
            return "custom"

    schema = PhysicalSchema(
        tables=(
            PhysicalTable(
                "orders", (PhysicalColumn("order_id", "bigint"),), ("order_id",)
            ),
            PhysicalTable(
                "invoices",
                (PhysicalColumn("invoice_id", "bigint"),),
                ("invoice_id",),
            ),
        )
    )
    result = fold(schema, infer_missing_keys=False, selector=SameName())
    names = [m.name for m in result.layer.models]

    assert len(names) == len(set(names))
    for name in names:
        assert result.layer.model(name) is not None


# ── 점수의 안정성 ─────────────────────────────────────────────────────────────


def test_a_tables_score_does_not_depend_on_another_tables_statistics():
    """행 수를 모르는 자리를 상수 0.5 로 메우던 동안, 스키마 안의 *다른*
    테이블 하나가 통계를 갖고 있느냐에 따라 같은 테이블이 FACT 와 DIMENSION
    사이를 오갔다. 자기 자신에 대해 달라진 것이 없는데도.
    """
    from tablefold.classify import profile_tables

    from tablefold.graph import SchemaGraph as SG

    orders = PhysicalTable(
        name="orders",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("cust_id", "bigint"),
            PhysicalColumn("n1", "bigint"),
            PhysicalColumn("c1", "varchar"),
            PhysicalColumn("c2", "varchar"),
            PhysicalColumn("c3", "varchar"),
        ),
        primary_key=("id",),
    )
    fk = ForeignKey("orders", ("cust_id",), "customers", ("cust_id",))

    def score_with(rows):
        customers = PhysicalTable(
            name="customers",
            columns=(PhysicalColumn("cust_id", "bigint", nullable=False),),
            primary_key=("cust_id",),
            row_estimate=rows,
        )
        graph = SG.build(PhysicalSchema((orders, customers), (fk,)))
        return next(p for p in profile_tables(graph) if p.name == "orders")

    with_stats = score_with(10_000)
    without = score_with(None)

    assert with_stats.score == without.score
    assert with_stats.role == without.role


# ── 별칭 면제의 경계 ──────────────────────────────────────────────────────────


def test_an_alias_cannot_smuggle_an_unknown_field_into_where(retail_schema):
    """출력 별칭은 ``ORDER BY`` 에서만 참조할 수 있다. ``WHERE`` 에 쓴 이름까지
    면제하면 미지 컬럼 검사가 뚫리고, 실패는 데이터베이스에서 난다."""
    result = fold(retail_schema, policy=SelectionPolicy(max_areas=4))

    with pytest.raises(ExpansionError, match="unknown fields"):
        expand(
            "SELECT grand_total AS revenue FROM orders WHERE revenue > 1",
            result.layer,
            result.graph,
        )


def test_an_alias_in_order_by_is_still_allowed(retail_schema):
    result = fold(retail_schema, policy=SelectionPolicy(max_areas=4))

    sql = expand(
        "SELECT grand_total AS revenue FROM orders ORDER BY revenue",
        result.layer,
        result.graph,
    ).sql

    assert "revenue" in sql


# ── 예약어 ────────────────────────────────────────────────────────────────────


def test_reserved_words_are_quoted():
    """``FROM user AS base`` 나 ``AS table`` 은 데이터베이스가 문법 오류로 읽는다."""
    reserved = PhysicalTable(
        name="order",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("select", "varchar"),
        ),
        primary_key=("id",),
    )
    other = PhysicalTable(
        name="user",
        columns=(
            PhysicalColumn("id", "bigint", nullable=False),
            PhysicalColumn("order_id", "bigint"),
            PhysicalColumn("table", "varchar"),
        ),
        primary_key=("id",),
    )
    schema = PhysicalSchema(
        tables=(reserved, other),
        foreign_keys=(ForeignKey("user", ("order_id",), "order", ("id",)),),
    )
    result = fold(schema, infer_missing_keys=False)
    model = next(m for m in result.layer.models if m.base_table == "user")
    field = next(f for f in model.fields if f.source.column == "select")

    sql = expand(
        f'SELECT {field.name} FROM "{model.name}"', result.layer, result.graph
    ).sql

    assert '"user"' in sql
    assert '"order"' in sql
    assert '"select"' in sql


# ── 앵커 목록의 낭비 ──────────────────────────────────────────────────────────


def test_pruning_drops_anchors_that_buy_nothing(retail_schema):
    """앵커가 사는 것은 테이블이 아니라 *조합* 이다.

    팩트와 차원을 모두 앵커로 주면 대개 절반이 낭비다 — NL2SQL 에서 팩트 앵커
    10개 중 5개는 답할 수 있는 질문을 하나도 늘리지 않았다. 빼도 답변가능률은
    그대로여야 하고, 모델과 프롬프트만 줄어야 한다.
    """
    from tablefold.clustering.select import ExplicitSelector
    from tablefold.presentation import emit
    from tablefold.presentation import fidelity as fid

    every = [t.name for t in retail_schema.tables]

    def layer(prune):
        result = fold(
            retail_schema,
            infer_missing_keys=False,
            selector=ExplicitSelector(every, prune_redundant=prune),
            policy=SelectionPolicy(max_areas=len(every)),
            field_budget=10_000,
            max_hops=1,
        )
        return result, fid.measure(result.layer, result.graph)

    full, full_fid = layer(False)
    lean, lean_fid = layer(True)

    assert len(lean.layer.models) < len(full.layer.models)
    assert lean_fid.pair_answerability == full_fid.pair_answerability
    assert len(emit.render_text(lean.layer)) < len(emit.render_text(full.layer))


def test_pruning_keeps_every_table_covered(retail_schema):
    """이웃이 없는 테이블은 어떤 쌍에도 안 들어간다.

    쌍만 보고 빼면 그런 테이블을 유일하게 담은 앵커가 조용히 사라진다.
    """
    from tablefold.clustering.select import ExplicitSelector
    from tablefold.presentation import fidelity as fid

    every = [t.name for t in retail_schema.tables]
    result = fold(
        retail_schema,
        infer_missing_keys=False,
        selector=ExplicitSelector(every, prune_redundant=True),
        policy=SelectionPolicy(max_areas=len(every)),
        field_budget=10_000,
        max_hops=1,
    )
    measured = fid.measure(result.layer, result.graph)
    covered = {
        t.lower()
        for m in result.layer.models
        for t in (m.base_table, *m.absorbed_tables)
    }

    assert len(covered) == len(retail_schema.tables)
    assert measured.pair_answerability == 1.0


def test_pruning_is_off_by_default(retail_schema):
    """호출자가 이름을 지목했으면 기본값은 그대로 두는 것이다."""
    from tablefold.clustering.select import ExplicitSelector

    names = [t.name for t in retail_schema.tables[:5]]
    result = fold(
        retail_schema,
        infer_missing_keys=False,
        selector=ExplicitSelector(names),
        policy=SelectionPolicy(max_areas=len(names)),
    )

    assert [m.base_table for m in result.layer.models] == names
