"""자동 탐색.

두 가지가 지켜져야 쓸모가 있다.

* **같은 자** — 모든 후보를 같은 점수 공식으로 잰다. 전에는 스타 프리셋이
  ``pair*60 + absorption*40`` 으로, 그리드가 가중합으로 매겨져서 어느 쪽이
  이기는지를 스키마가 아니라 공식이 정했다. 스타의 점수는 아예 상수 98.5 였다.
* **재현 가능** — 돌려준 설정으로 다시 접으면 잰 것과 같은 레이어가 나온다.
  아니면 화면이 A 의 점수를 띄우고 B 를 그린다.
"""

from __future__ import annotations

import json

import pytest

from tablefold.choose.autotune import (
    COMPACT_TARGET,
    WEIGHTS,
    _compactness,
    _grid,
    _sample,
    autotune,
    autotune_stream,
    score_layer,
)
from tablefold.fold import fold


@pytest.fixture
def tuned(retail_schema):
    return autotune(retail_schema, max_candidates=8)


def test_the_reported_score_is_the_score_of_the_reported_layer(tuned):
    """점수와 레이어가 따로 놀면 화면이 A 를 말하고 B 를 그린다."""
    again, _, _ = score_layer(tuned.result)

    assert round(again, 1) == tuned.score


def test_the_star_preset_is_scored_not_asserted(retail_schema):
    """전에는 스타면 무조건 98.5 였다. 무엇을 접든 같은 값이 나왔다."""
    from tablefold.relate.synthesize import add_period_anchor
    from tablefold.t2sql.preset import fold_star_schema, recover_relationships

    prepared = add_period_anchor(recover_relationships(retail_schema))
    star = fold_star_schema(prepared)
    expected, _, _ = score_layer(star)

    result = autotune(retail_schema, max_candidates=1)

    assert result.anchor_mode == "star"
    assert round(result.score, 1) == round(expected, 1)
    assert result.score != 98.5


def test_the_weights_sum_to_one():
    assert round(sum(WEIGHTS.values()), 6) == 1.0


def test_compactness_discriminates_at_warehouse_sizes():
    """``100 - length/200`` 은 2만 자에서 음수라 0 으로 잘렸다 — 죽은 항이었다."""
    assert _compactness(COMPACT_TARGET) == 100.0
    assert _compactness(21_000) > 0.0
    assert _compactness(21_000) > _compactness(30_000)


def test_the_grid_is_sampled_evenly_not_truncated():
    """중첩 ``break`` 로 자르면 가장 바깥 축이 첫 값에 고정된다."""
    picked = _sample(_grid(), 8)

    assert len(picked) == 8
    assert len({c.coverage for c in picked}) > 1


def test_every_candidate_is_a_real_fold(retail_schema):
    """평가 수가 실제로 접어 본 횟수여야 한다."""
    events = list(autotune_stream(retail_schema, max_candidates=6))
    progress = [e for e in events if e["event"] == "progress"]
    done = events[-1]

    assert done["event"] == "done"
    assert len(progress) == done["result"]["candidates_evaluated"]


def test_stream_events_are_json_serialisable(retail_schema):
    """화면이 NDJSON 으로 받는다. 파이썬 객체가 섞이면 스트림이 죽는다."""
    for event in autotune_stream(retail_schema, max_candidates=3):
        payload = {k: v for k, v in event.items() if not k.startswith("_")}
        json.dumps(payload)


def test_autotune_matches_its_own_stream(retail_schema):
    """두 벌로 두었을 때 한쪽만 고쳐져서 화면과 CLI 의 추천이 갈라졌다."""
    streamed = [
        e
        for e in autotune_stream(retail_schema, max_candidates=5)
        if e["event"] == "done"
    ][0]["result"]
    direct = autotune(retail_schema, max_candidates=5)

    assert streamed["score"] == direct.score
    assert streamed["anchor_mode"] == direct.anchor_mode


def test_a_layer_that_abandons_tables_cannot_win_on_the_other_axes(retail_schema):
    """표를 버려서 점수를 사면 안 된다."""
    starved = fold(retail_schema, field_budget=20, infer_missing_keys=True)
    generous = fold(retail_schema, field_budget=600, infer_missing_keys=True)

    assert score_layer(starved)[0] < score_layer(generous)[0]
