from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from tablefold import emit
from tablefold.cli import app
from tablefold.cluster import SelectionPolicy
from tablefold.ir import FieldKind
from tablefold.pipeline import fold
from tests.conftest import FIXTURES

runner = CliRunner()


@pytest.fixture(scope="module")
def retail_fold(retail_schema):
    return fold(retail_schema, policy=SelectionPolicy(max_areas=4))


# ── pipeline ──────────────────────────────────────────────────────────────────


def test_fold_produces_the_whole_chain(retail_fold):
    assert len(retail_fold.layer.models) == 4
    assert retail_fold.profiles
    assert retail_fold.clustering.areas
    assert retail_fold.inferred_foreign_keys == 0


def test_fold_recovers_a_schema_with_no_declared_keys(retail_schema):
    from dataclasses import replace

    stripped = replace(retail_schema, foreign_keys=())
    result = fold(stripped, policy=SelectionPolicy(max_areas=4))

    assert result.inferred_foreign_keys > 40
    assert len(result.layer.models) == 4
    # The fold is only useful if the recovered graph still yields wide models.
    assert max(len(m.fields) for m in result.layer.models) > 20


def test_inference_can_be_switched_off(retail_schema):
    from dataclasses import replace

    stripped = replace(retail_schema, foreign_keys=())
    result = fold(
        stripped, policy=SelectionPolicy(max_areas=4), infer_missing_keys=False
    )

    assert result.inferred_foreign_keys == 0
    assert result.layer.models == ()


# ── emit ──────────────────────────────────────────────────────────────────────


def test_dict_carries_every_field_and_its_provenance(retail_fold):
    payload = emit.to_dict(retail_fold.layer)

    assert payload["source_table_count"] == 53
    assert payload["model_count"] == 4

    orders = next(m for m in payload["models"] if m["name"] == "orders")
    assert len(orders["fields"]) == len(retail_fold.layer.model("orders").fields)

    aggregated = next(
        f for f in orders["fields"] if f["kind"] == FieldKind.AGGREGATED.value
    )
    assert aggregated["source"]["aggregate"]
    assert aggregated["source"]["path"][0]["cardinality"] == "one_to_many"


def test_yaml_and_json_round_trip(retail_fold):
    payload = emit.to_dict(retail_fold.layer)

    assert yaml.safe_load(emit.to_yaml(retail_fold.layer)) == payload
    assert json.loads(emit.to_json(retail_fold.layer)) == payload


def test_prompt_text_groups_fields_by_kind(retail_fold):
    text = emit.render_text(retail_fold.layer)

    assert "Own columns:" in text
    assert "Joined in (one related row each):" in text
    assert "Aggregated from child rows:" in text


def test_prompt_text_fits_a_context_window(retail_fold):
    """The entire 53-table schema has to be readable in one shot.

    A rough four-characters-per-token estimate is enough to catch the fold
    regressing back toward dumping the raw schema.
    """
    text = emit.render_text(retail_fold.layer)

    assert len(text) // 4 < 4000


def test_report_names_every_model(retail_fold):
    report = emit.render_report(retail_fold.layer)

    for model in retail_fold.layer.models:
        assert model.name in report


# ── cli ───────────────────────────────────────────────────────────────────────


def test_cli_fold_reports_coverage_and_why_it_stopped():
    """No model count is requested, so the report has to justify the one it chose."""
    result = runner.invoke(app, ["fold", "--ddl", str(FIXTURES / "retail_50.sql")])

    assert result.exit_code == 0
    assert "53 tables ->" in result.stdout
    assert "covered" in result.stdout
    assert "stopped:" in result.stdout


def test_cli_min_gain_shrinks_the_layer():
    def model_count(min_gain: str) -> int:
        result = runner.invoke(
            app,
            [
                "fold",
                "--ddl",
                str(FIXTURES / "retail_50.sql"),
                "--min-gain",
                min_gain,
                "-f",
                "json",
            ],
        )
        assert result.exit_code == 0
        return json.loads(result.stdout)["model_count"]

    assert model_count("3") < model_count("1")


def test_cli_max_cost_declines_expensive_anchors():
    def layer(max_cost: str) -> dict:
        result = runner.invoke(
            app,
            [
                "fold",
                "--ddl",
                str(FIXTURES / "retail_50.sql"),
                "--max-cost",
                max_cost,
                "-f",
                "json",
            ],
        )
        assert result.exit_code == 0
        return json.loads(result.stdout)

    cheap = layer("10")
    unlimited = layer("1000")

    assert cheap["model_count"] < unlimited["model_count"]
    assert cheap["covered_table_count"] < unlimited["covered_table_count"]


def test_cli_max_models_still_caps_the_layer():
    result = runner.invoke(
        app,
        ["fold", "--ddl", str(FIXTURES / "retail_50.sql"), "-n", "4", "-f", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["model_count"] == 4


def test_cli_fold_writes_yaml(tmp_path):
    out = tmp_path / "layer.yaml"
    result = runner.invoke(
        app,
        [
            "fold",
            "--ddl",
            str(FIXTURES / "retail_50.sql"),
            "-o",
            str(out),
            "-f",
            "yaml",
        ],
    )

    assert result.exit_code == 0
    payload = yaml.safe_load(out.read_text())
    assert payload["model_count"] > 0
    assert payload["covered_table_count"] == 53 - len(payload["notes"])
    assert payload["stop_reason"]


def test_cli_expand_emits_runnable_sql():
    result = runner.invoke(
        app,
        [
            "expand",
            "SELECT SUM(grand_total) AS revenue FROM orders",
            "--ddl",
            str(FIXTURES / "retail_50.sql"),
        ],
    )

    assert result.exit_code == 0
    assert "FROM orders AS base" in result.stdout
    assert "WITH tf__orders AS" in result.stdout


def test_cli_expand_reports_a_bad_field():
    result = runner.invoke(
        app,
        [
            "expand",
            "SELECT no_such_field FROM orders",
            "--ddl",
            str(FIXTURES / "retail_50.sql"),
        ],
    )

    assert result.exit_code == 1


def test_cli_requires_a_source():
    result = runner.invoke(app, ["fold"])

    assert result.exit_code == 2


def test_cli_rejects_two_sources():
    result = runner.invoke(
        app, ["fold", "--ddl", str(FIXTURES / "retail_50.sql"), "--dsn", "postgres://x"]
    )

    assert result.exit_code == 2


def test_cli_rejects_an_unknown_format():
    result = runner.invoke(
        app, ["fold", "--ddl", str(FIXTURES / "retail_50.sql"), "-f", "toml"]
    )

    assert result.exit_code == 2


def test_cli_inspect_shows_the_profile():
    result = runner.invoke(app, ["inspect", "--ddl", str(FIXTURES / "retail_50.sql")])

    assert result.exit_code == 0
    assert "orders" in result.stdout
    assert "fact" in result.stdout
