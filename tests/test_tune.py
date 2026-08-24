"""골드셋을 목적함수로 쓰는 교환곡선.

노브가 존재하는 이유는 목적함수가 없어서다. 골드셋이 있으면 "얼마를 줄까"가
튜닝이 아니라 **비용 대 커버리지**의 결정이 된다.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tablefold.build.compose import ComposeOptions, compose
from tablefold.choose import tune
from tablefold.choose.classify import profile_tables
from tablefold.choose.cluster import SelectionPolicy, cluster
from tablefold.report import answerable


@dataclass(frozen=True)
class FakeCase:
    """:class:`~tablefold.t2sql.goldset.GoldCase` 의 최소 대역.

    ``answerable`` 이 그 타입을 import 하지 않는 이유(순환)를 테스트도 따른다 —
    필요한 것은 ``case_id`` · ``subject`` · ``gold_references`` 뿐이다.
    """

    case_id: str
    gold_references: dict[str, frozenset[str]]

    @property
    def subject(self) -> str:
        return self.case_id.split("_", 1)[0]


@pytest.fixture
def layer(retail_graph):
    clustering = cluster(
        retail_graph, profile_tables(retail_graph), policy=SelectionPolicy(max_areas=4)
    )
    return compose(retail_graph, clustering, options=ComposeOptions(field_budget=400))


def test_a_case_the_layer_covers_is_answerable(layer, retail_graph):
    model = next(m for m in layer.models if m.fields)
    field = model.fields[0]
    case = FakeCase(
        "SA_0001",
        {field.source.table.lower(): frozenset({field.source.column.lower()})},
    )

    report = answerable.measure(layer, (case,), retail_graph)

    assert report.answered == 1
    assert report.verdicts[0].model is not None


def test_a_column_that_is_nowhere_is_not_answerable(layer, retail_graph):
    case = FakeCase("SA_0002", {"orders": frozenset({"no_such_column"})})

    report = answerable.measure(layer, (case,), retail_graph)

    assert report.answered == 0
    assert report.verdicts[0].missing == ("orders.no_such_column",)


def test_join_keys_count_as_available(layer, retail_graph):
    """키는 필드로 **일부러** 안 내놓는다. 결손으로 세면 자동 조인을 결손으로 센다.

    이 한 가지 때문에 실측 골드셋 50건이 전부 0 으로 나왔다.
    """
    orders = retail_graph.schema.table("orders")
    key = orders.primary_key[0]

    assert layer.model("orders").field(key) is None or True  # 필드 여부와 무관
    case = FakeCase("SA_0003", {"orders": frozenset({key.lower()})})

    assert answerable.measure(layer, (case,), retail_graph).answered == 1


def test_subjects_are_counted_separately_from_cases(layer, retail_graph):
    """건수 비율은 질문이 몰린 주제에 끌려간다."""
    good = FakeCase("SA_0001", {"orders": frozenset({"id"})})
    bad = FakeCase("FI_0001", {"orders": frozenset({"nope"})})

    report = answerable.measure(layer, (good, good, good, bad), retail_graph)

    assert report.answered == 3
    assert report.answered_subjects == 1
    assert dict((s, a) for s, a, _ in report.subjects) == {"SA": 3, "FI": 0}


# ── 곡선 ──────────────────────────────────────────────────────────────────────


def test_the_curve_is_monotone_in_the_budget(retail_schema):
    cases = (
        FakeCase("SA_0001", {"orders": frozenset({"total"})}),
        FakeCase("SA_0002", {"customers": frozenset({"email"})}),
    )
    points = tune.curve(retail_schema, cases, budgets=(2_000, 8_000, 20_000))

    assert [p.prompt_budget for p in points] == [2_000, 8_000, 20_000]
    # 예산을 늘려서 답이 줄어들면 배분이 잘못된 것이다.
    answered = [p.answered for p in points]
    assert answered == sorted(answered)


def test_every_point_respects_its_budget(retail_schema):
    cases = (FakeCase("SA_0001", {"orders": frozenset({"total"})}),)
    # 예산은 모델 헤더 고정비보다 커야 의미가 있다. 간선 없는 참조 표
    # (currencies · exchange_rates · audit_events)도 앵커로 세우므로 최소
    # 레이어가 커졌다 — 그 아래 예산은 "답이 잘리는 점"이지 "잘못된 배분"이
    # 아니다. 가능한 예산에서만 배분 규율을 본다.
    for point in tune.curve(retail_schema, cases, budgets=(6_000, 12_000)):
        assert point.prompt_length <= point.prompt_budget


def test_the_knee_is_the_cheapest_point_that_answers_the_most():
    points = (
        tune.CurvePoint(1_000, 900, 2, 10, 1, 5, 1, 2),
        tune.CurvePoint(2_000, 1_900, 2, 20, 4, 5, 2, 2),
        tune.CurvePoint(4_000, 3_800, 2, 40, 4, 5, 2, 2),
    )

    best = tune.knee(points)

    assert best is not None
    assert best.prompt_budget == 2_000


def test_the_knee_of_nothing_is_nothing():
    assert tune.knee(()) is None


def test_the_rendered_curve_names_the_recommendation(retail_schema):
    cases = (FakeCase("SA_0001", {"orders": frozenset({"total"})}),)
    text = tune.render(tune.curve(retail_schema, cases, budgets=(3_000, 9_000)))

    assert "추천" in text
    assert "포화" in text
