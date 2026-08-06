from __future__ import annotations

import json

import pytest

from tablefold.build.compose import compose
from tablefold.choose.classify import profile_tables
from tablefold.choose.cluster import build_lattice, cluster
from tablefold.choose.select import (
    Choice,
    GreedySelector,
    LLMSelector,
    Selection,
    SelectionError,
    SelectionPolicy,
    StopReason,
    _parse_choices,
)


@pytest.fixture
def lattice(retail_graph):
    return build_lattice(retail_graph, profile_tables(retail_graph))


def _replies(text: str):
    """A completer that answers with *text* and records what it was asked."""
    seen: list[str] = []

    def complete(prompt: str) -> str:
        seen.append(prompt)
        return text

    complete.prompts = seen  # type: ignore[attr-defined]
    return complete


def _anchors(payload: list) -> str:
    return json.dumps({"anchors": payload})


# ── the lattice ───────────────────────────────────────────────────────────────


def test_every_table_is_a_candidate(lattice, retail_graph):
    assert len(lattice.candidates) == len(retail_graph.schema.tables)
    assert lattice.total_table_count == 53


def test_lattice_lookup_is_case_insensitive(lattice):
    assert lattice.get("ORDERS") is not None
    assert lattice.get("no_such_table") is None


def test_rendered_lattice_carries_what_a_semantic_call_needs(lattice):
    """An anchor's name says little; what it absorbs says what the model is."""
    rendered = lattice.render(limit=10)

    assert "orders" in rendered
    assert "order_items" in rendered  # a member, not just an anchor
    assert len(rendered.splitlines()) == 13  # header, blank, columns, 10 rows


# ── parsing ───────────────────────────────────────────────────────────────────


def test_parses_objects_and_bare_strings():
    parsed = _parse_choices(
        _anchors([{"table": "orders", "name": "sales"}, "products"])
    )

    assert [(c.anchor, c.name) for c in parsed] == [
        ("orders", "sales"),
        ("products", None),
    ]


def test_parses_through_prose_and_fences():
    reply = 'Here you go:\n```json\n{"anchors": ["orders"]}\n```\nHope that helps.'

    assert [c.anchor for c in _parse_choices(reply)] == ["orders"]


@pytest.mark.parametrize(
    "reply",
    ["not json at all", "{not: valid}", '{"models": ["orders"]}'],
)
def test_unusable_replies_are_rejected(reply):
    with pytest.raises(SelectionError):
        _parse_choices(reply)


def test_blank_names_fall_back_to_the_table_name():
    parsed = _parse_choices(_anchors([{"table": "orders", "name": "   "}]))

    assert parsed[0].name is None


# ── selection ─────────────────────────────────────────────────────────────────


def test_llm_choice_is_honoured(lattice):
    selector = LLMSelector(_replies(_anchors(["orders", "customers"])))

    selection = selector.select(lattice, SelectionPolicy())

    assert [c.anchor for c in selection.choices] == ["orders", "customers"]
    assert selection.stop_reason is StopReason.SELECTOR_CHOSE
    assert selection.label == "llm"


def test_the_prompt_states_the_policy(lattice):
    complete = _replies(_anchors(["orders"]))
    LLMSelector(complete).select(
        lattice, SelectionPolicy(coverage_target=0.8, min_gain=3)
    )

    prompt = complete.prompts[0]  # type: ignore[attr-defined]
    assert "80%" in prompt
    assert "at least 3 tables" in prompt


def test_invented_tables_are_dropped_not_trusted(lattice):
    """A hallucinated name would otherwise fail in compose, far from its source."""
    selector = LLMSelector(_replies(_anchors(["orders", "ghost_table", "customers"])))

    selection = selector.select(lattice, SelectionPolicy())

    assert [c.anchor for c in selection.choices] == ["orders", "customers"]


def test_names_are_resolved_to_the_schema_spelling(lattice):
    selector = LLMSelector(_replies(_anchors(["ORDERS", "Orders"])))

    selection = selector.select(lattice, SelectionPolicy())

    # Resolved to the real spelling, and the duplicate dropped.
    assert [c.anchor for c in selection.choices] == ["orders"]


def test_the_ceiling_still_binds_an_llm(lattice):
    selector = LLMSelector(_replies(_anchors(["orders", "customers", "products"])))

    selection = selector.select(lattice, SelectionPolicy(max_areas=2))

    assert len(selection.choices) == 2


@pytest.mark.parametrize("reply", ["", "sorry, I cannot help", _anchors(["ghost"])])
def test_an_unusable_completion_falls_back_to_greedy(lattice, reply):
    """A malformed completion must not cost the caller their fold."""
    selection = LLMSelector(_replies(reply)).select(lattice, SelectionPolicy())

    assert selection.choices
    assert selection.label == "greedy (llm fallback)"
    assert [c.anchor for c in selection.choices] == [
        c.anchor for c in GreedySelector().select(lattice, SelectionPolicy()).choices
    ]


def test_a_custom_fallback_is_honoured(lattice):
    class FixedSelector:
        def select(self, lattice, policy):
            return Selection((Choice("customers"),), StopReason.SELECTOR_CHOSE, "fixed")

    selection = LLMSelector(_replies("garbage"), fallback=FixedSelector()).select(
        lattice, SelectionPolicy()
    )

    assert [c.anchor for c in selection.choices] == ["customers"]
    assert selection.label == "fixed (llm fallback)"


# ── through the pipeline ──────────────────────────────────────────────────────


def test_a_chosen_set_is_re_measured_not_trusted(retail_graph):
    """The selector picks; the graph decides what that covered."""
    profiles = profile_tables(retail_graph)
    selector = LLMSelector(_replies(_anchors(["orders", "products"])))

    result = cluster(retail_graph, profiles, selector=selector)

    assert [a.anchor for a in result.areas] == ["orders", "products"]
    assert result.covered_table_count == 29
    assert result.areas[0].new_tables == 16
    assert result.areas[1].new_tables == 13  # marginal, not its full reach of 16
    assert result.selector == "llm"


def test_a_model_can_be_named_for_what_it_is_about(retail_graph):
    profiles = profile_tables(retail_graph)
    selector = LLMSelector(
        _replies(_anchors([{"table": "invoice_lines", "name": "billing"}]))
    )

    result = cluster(retail_graph, profiles, selector=selector)
    layer = compose(retail_graph, result)

    model = layer.model("billing")
    assert model is not None
    # Renaming is cosmetic: the grain, and what expansion reads from, do not move.
    assert model.base_table == "invoice_lines"
    assert layer.model("invoice_lines") is None


def test_fold_takes_a_selector(retail_schema):
    """The whole pipeline, with the choice made from outside it."""
    from tablefold.fold import fold

    result = fold(
        retail_schema,
        selector=LLMSelector(
            _replies(_anchors([{"table": "orders", "name": "sales"}, "products"]))
        ),
    )

    assert [m.name for m in result.layer.models] == ["sales", "products"]
    assert result.layer.selector == "llm"
    assert result.layer.covered_table_count == 29


def test_cli_reports_a_missing_llm_extra_instead_of_folding():
    """Asking for `--llm` without the extra must fail loudly, not fold silently."""
    from typer.testing import CliRunner

    from tablefold.cli import app
    from tests.conftest import FIXTURES

    try:
        import anthropic  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - only when the extra is installed
        pytest.skip("the llm extra is installed")

    result = CliRunner().invoke(
        app, ["fold", "--ddl", str(FIXTURES / "retail_50.sql"), "--llm"]
    )

    assert result.exit_code == 2


def test_the_layer_records_who_chose(retail_graph):
    profiles = profile_tables(retail_graph)

    greedy = compose(retail_graph, cluster(retail_graph, profiles))
    llm = compose(
        retail_graph,
        cluster(
            retail_graph,
            profiles,
            selector=LLMSelector(_replies(_anchors(["orders"]))),
        ),
    )

    assert greedy.selector == "greedy"
    assert llm.selector == "llm"


# ── 중복 앵커 제거 ────────────────────────────────────────────────────────────


def _candidate(name: str, reach: set[str], inlined: set[str]):
    from tablefold.choose.select import Candidate

    return Candidate(
        name=name,
        role="dimension",
        score=0.2,
        reach=frozenset(reach),
        estimated_fields=20,
        inlined=frozenset(inlined),
    )


def test_pruning_keeps_the_only_anchor_that_can_group_by_a_table():
    """도달 가능과 그룹핑 가능은 다르다.

    ``D_CUSTOMER`` 는 ``D_SA_ORG`` 앵커 안에도 들어 있지만, 거기서는 1:N 자식이라
    컬럼이 ``filter_only`` 로만 나온다 — WHERE 는 되고 GROUP BY 는 안 된다.
    "거래처별" 질문은 GROUP BY 를 요구하므로 그 앵커로는 답이 없다.

    쌍만 보고 자르면 이 앵커가 조용히 사라지고, 답변가능률은 100% 를 유지한 채로
    골드셋 BD 4건이 실패한다.
    """
    from tablefold.choose.select import _drop_redundant

    sales_org = _candidate(
        "D_SA_ORG",
        reach={"d_sa_org", "f_customer_bond", "d_customer"},
        inlined={"d_sa_org"},
    )
    customer = _candidate(
        "D_CUSTOMER",
        reach={"d_customer", "d_sa_org"},
        inlined={"d_customer", "d_sa_org"},
    )

    kept = {c.name for c in _drop_redundant([sales_org, customer])}

    assert kept == {"D_SA_ORG", "D_CUSTOMER"}


def test_pruning_still_drops_an_anchor_that_buys_nothing():
    """투영까지 봐도 진짜 중복은 중복이다."""
    from tablefold.choose.select import _drop_redundant

    wide = _candidate(
        "D_ORG", reach={"d_org", "d_team", "d_dept"}, inlined={"d_org", "d_team"}
    )
    narrow = _candidate("D_TEAM", reach={"d_team", "d_org"}, inlined={"d_team"})

    kept = {c.name for c in _drop_redundant([wide, narrow])}

    assert kept == {"D_ORG"}


def test_the_lattice_prices_projectable_reach_separately(retail_graph):
    """``inlined`` 는 그래프에서 나온다 — 앵커 자신 + 다대일로 닿는 표."""
    from tablefold.choose.cluster import build_lattice

    lattice = build_lattice(retail_graph, profile_tables(retail_graph), max_hops=3)
    orders = lattice.get("orders")

    assert orders is not None
    assert "orders" in orders.inlined
    assert orders.inlined <= orders.reach
    # 1:N 자식은 집계로만 들어오므로 투영 범위에 없다.
    assert orders.inlined != orders.reach
