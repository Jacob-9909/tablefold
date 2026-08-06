"""CLI(Command Line Interface) 커맨드 라인 모듈."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from tablefold.choose.cluster import SelectionPolicy
from tablefold.fold import FoldResult, fold
from tablefold.ir import PhysicalSchema
from tablefold.read.ddl import DDLIntrospector
from tablefold.report import prompt as emit
from tablefold.rewrite.expand import ExpansionError, expand

app = typer.Typer(
    add_completion=False,
    help="Fold a wide physical schema into as few wide logical models as it takes.",
)

DdlOption = Annotated[
    Path | None, typer.Option("--ddl", help="Path to a DDL script to read.")
]
MssqlOption = Annotated[
    bool,
    typer.Option(
        "--mssql",
        help="Read the schema from SQL Server using TABLEFOLD_MSSQL_* env vars, "
        "and validate recovered relationships against the data.",
    ),
]
DsnOption = Annotated[
    str | None,
    typer.Option(
        "--dsn", help="PostgreSQL connection string (needs the postgres extra)."
    ),
]
SchemaOption = Annotated[str, typer.Option("--schema", help="Database schema name.")]
CoverageOption = Annotated[
    float,
    typer.Option(
        "--coverage",
        min=0.0,
        max=1.0,
        help="Fraction of tables the models should jointly cover.",
    ),
]
MinGainOption = Annotated[
    int,
    typer.Option(
        "--min-gain",
        min=1,
        help="New tables an extra model must bring to be worth its tokens. "
        "Raise it for a smaller layer, lower it for more coverage.",
    ),
]
MaxCostOption = Annotated[
    float,
    typer.Option(
        "--max-cost",
        min=0.1,
        help="Fields a model may spend per table it is the first to cover. "
        "Declines anchors that overlap an existing model almost entirely.",
    ),
]
LlmOption = Annotated[
    bool,
    typer.Option(
        "--llm",
        help="Let a language model choose the anchors and name the models. "
        "Needs the 'llm' extra and ANTHROPIC_API_KEY. Falls back to greedy if "
        "the completion is unusable.",
    ),
]
MaxModelsOption = Annotated[
    int | None,
    typer.Option(
        "--max-models",
        "-n",
        min=1,
        help="Hard ceiling on model count. Unset by default — the count is "
        "normally an output of --coverage and --min-gain.",
    ),
]
HopsOption = Annotated[
    int,
    typer.Option("--max-hops", min=1, help="How far to follow foreign keys forwards."),
]
FieldsOption = Annotated[
    int,
    typer.Option(
        "--field-budget",
        min=1,
        help="Fields the whole layer may spend, shared across models.",
    ),
]


@app.command("fold")
def fold_command(
    ddl: DdlOption = None,
    dsn: DsnOption = None,
    schema: SchemaOption = "public",
    coverage: CoverageOption = 0.90,
    min_gain: MinGainOption = 2,
    max_cost: MaxCostOption = 10.0,
    llm: LlmOption = False,
    max_models: MaxModelsOption = None,
    max_hops: HopsOption = 3,
    field_budget: FieldsOption = 200,
    output: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write the layer to this path.")
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="yaml | json | text | report")
    ] = "report",
) -> None:
    """물리 스키마를 읽어 와이드 논리 레이어로 Fold(압축)하고 결과를 출력합니다."""
    result = _run_fold(
        ddl=ddl,
        dsn=dsn,
        schema=schema,
        coverage=coverage,
        min_gain=min_gain,
        max_cost=max_cost,
        llm=llm,
        max_models=max_models,
        max_hops=max_hops,
        field_budget=field_budget,
    )
    rendered = _render(result, output_format)

    if output is not None:
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"wrote {output}")
        typer.echo(emit.render_report(result.layer))
    else:
        typer.echo(rendered)


@app.command("inspect")
def inspect_command(
    ddl: DdlOption = None,
    dsn: DsnOption = None,
    schema: SchemaOption = "public",
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 25,
) -> None:
    """Show the fact/dimension profile the fold is based on."""
    physical = _load_schema(ddl=ddl, dsn=dsn, schema=schema)
    result = fold(physical)

    typer.echo(
        f"{len(physical.tables)} tables, "
        f"{len(result.schema.foreign_keys)} foreign keys "
        f"({result.inferred_foreign_keys} inferred)"
    )
    typer.echo(
        f"{'table':<28}{'role':<12}{'score':>7}{'measure':>9}"
        f"{'time':>6}{'out':>5}{'in':>5}"
    )
    for profile in result.profiles[:limit]:
        typer.echo(
            f"{profile.name:<28}{profile.role.value:<12}{profile.score:>7.3f}"
            f"{profile.measure_density:>9.2f}{profile.temporal_count:>6}"
            f"{profile.out_degree:>5}{profile.in_degree:>5}"
        )


@app.command("expand")
def expand_command(
    sql: Annotated[str, typer.Argument(help="SQL written against the logical models.")],
    layer_path: Annotated[
        Path | None,
        typer.Option("--layer", "-l", help="A layer previously written by `fold`."),
    ] = None,
    ddl: DdlOption = None,
    dsn: DsnOption = None,
    schema: SchemaOption = "public",
    coverage: CoverageOption = 0.90,
    min_gain: MinGainOption = 2,
    max_cost: MaxCostOption = 10.0,
    llm: LlmOption = False,
    max_models: MaxModelsOption = None,
    max_hops: HopsOption = 3,
    field_budget: FieldsOption = 200,
    dialect: Annotated[
        str, typer.Option("--dialect", help="Target SQL dialect.")
    ] = "postgres",
) -> None:
    """Rewrite logical SQL into SQL the database can run.

    The physical schema is always required — expansion needs the real tables,
    not just the model definitions. ``--layer`` is therefore an optimisation
    (reuse an approved fold) rather than a substitute for ``--ddl`` / ``--dsn``.
    """
    if layer_path is not None:
        typer.secho(
            "note: --layer is not read yet; the layer is recomputed from the schema",
            fg=typer.colors.YELLOW,
            err=True,
        )

    result = _run_fold(
        ddl=ddl,
        dsn=dsn,
        schema=schema,
        coverage=coverage,
        min_gain=min_gain,
        max_cost=max_cost,
        llm=llm,
        max_models=max_models,
        max_hops=max_hops,
        field_budget=field_budget,
    )

    try:
        expansion = expand(sql, result.layer, result.graph, dialect=dialect)
    except ExpansionError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"-- models: {', '.join(expansion.models_used)} | "
        f"joins {expansion.joins_emitted}/{expansion.joins_available} "
        f"({expansion.joins_pruned} pruned)",
        fg=typer.colors.CYAN,
        err=True,
    )
    typer.echo(expansion.sql)


@app.command("context")
def context_command(
    ddl: DdlOption = None,
    dsn: DsnOption = None,
    schema: SchemaOption = "public",
    coverage: CoverageOption = 0.90,
    min_gain: MinGainOption = 2,
    max_cost: MaxCostOption = 10.0,
    llm: LlmOption = False,
    max_models: MaxModelsOption = None,
    max_hops: HopsOption = 3,
    field_budget: FieldsOption = 200,
) -> None:
    """Print the compact schema text intended for a prompt."""
    result = _run_fold(
        ddl=ddl,
        dsn=dsn,
        schema=schema,
        coverage=coverage,
        min_gain=min_gain,
        max_cost=max_cost,
        llm=llm,
        max_models=max_models,
        max_hops=max_hops,
        field_budget=field_budget,
    )
    text = emit.render_text(result.layer)
    typer.secho(
        f"-- {len(text)} chars, ~{len(text) // 4} tokens",
        fg=typer.colors.CYAN,
        err=True,
    )
    typer.echo(text)


@app.command("generate")
def generate_command(
    question: Annotated[
        str, typer.Argument(help="Natural language question to convert to SQL.")
    ],
    ddl: DdlOption = None,
    dsn: DsnOption = None,
    mssql: MssqlOption = False,
    schema: SchemaOption = "public",
    dialect: Annotated[
        str, typer.Option("--dialect", help="Target SQL dialect.")
    ] = "postgres",
    coverage: CoverageOption = 0.90,
    min_gain: MinGainOption = 2,
    max_cost: MaxCostOption = 10.0,
    llm: LlmOption = False,
    max_models: MaxModelsOption = None,
    max_hops: HopsOption = 3,
    field_budget: FieldsOption = 200,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Log each stage as it runs."),
    ] = False,
    trace: Annotated[
        bool,
        typer.Option("--trace", help="Print the full attempt trace afterwards."),
    ] = False,
    trace_full: Annotated[
        bool,
        typer.Option("--trace-full", help="Include whole prompts in the trace."),
    ] = False,
    star: Annotated[
        bool,
        typer.Option(
            "--star/--greedy",
            help="Anchor on every fact and dimension (default), or use the "
            "greedy set-cover fold. Star answers more questions; greedy is "
            "smaller.",
        ),
    ] = True,
    recover: Annotated[
        bool,
        typer.Option(
            "--recover/--no-recover",
            help="Recover undeclared relationships from primary keys. "
            "Warehouses rarely declare foreign keys.",
        ),
    ] = True,
    route: Annotated[
        bool,
        typer.Option(
            "--route/--no-route",
            help="Pick one model first (2 calls, smaller prompts), or show the "
            "whole layer at once (1 call).",
        ),
    ] = True,
) -> None:
    """Answer a question with SQL: pick a model, write logical SQL, expand it.

    Defaults differ from ``fold`` on purpose. ``fold`` is a general-purpose
    compressor, so it stays on the greedy set cover. This command exists to
    *answer questions*, and that needs the fold the measurements were taken
    with — every fact and dimension anchored, relationships recovered. Those
    were reachable only from ``demo/`` before, which is why the same question
    got a different answer here than in the benchmark.
    """
    import logging

    from tablefold.t2sql import (
        NL2SQL_EXAMPLES,
        GenerationError,
        fold_star_schema,
        generate_sql,
        recover_relationships,
    )
    from tablefold.t2sql.trace import enable_logging, render_trace

    if verbose or trace_full:
        enable_logging(
            logging.DEBUG if trace_full else logging.INFO, stream=sys.stderr
        )

    if star:
        if mssql:
            physical, how = _load_and_validate(schema, recover=recover)
            if dialect == "postgres":
                dialect = "tsql"
        else:
            physical = _load_schema(ddl=ddl, dsn=dsn, schema=schema)
            declared = len(physical.foreign_keys)
            if recover:
                physical = recover_relationships(physical)
            how = (
                f"foreign keys {declared} declared + "
                f"{len(physical.foreign_keys) - declared} recovered (schema only)"
            )
        result = fold_star_schema(physical, max_hops=max_hops)
        if not result.layer.models:
            typer.secho(
                "error: 레이어에 모델이 하나도 없다 — 질문할 대상이 없으므로 "
                "LLM 을 부르지 않는다. 스키마와 관계를 확인하라.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        typer.secho(
            f"-- {len(result.layer.models)} models, "
            f"{result.layer.field_count} fields, {how}",
            fg=typer.colors.CYAN,
            err=True,
        )
    else:
        result = _run_fold(
            ddl=ddl,
            dsn=dsn,
            schema=schema,
            coverage=coverage,
            min_gain=min_gain,
            max_cost=max_cost,
            llm=llm,
            max_models=max_models,
            max_hops=max_hops,
            field_budget=field_budget,
        )

    try:
        # 예시는 스키마에 안 맞으면 `valid_examples` 가 전부 뺀다. 다른 스키마에
        # 없는 필드 이름을 가르칠 위험 없이 기본으로 넣어 둘 수 있는 이유다.
        gen_res = generate_sql(
            question,
            result,
            dialect=dialect,
            examples=NL2SQL_EXAMPLES,
            route=route,
        )
    except GenerationError as exc:
        # 실패했을 때가 추적이 제일 필요한 때다. 요청하지 않아도 낸다.
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        typer.echo(render_trace(exc, full=trace_full), err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if trace or trace_full:
        typer.secho("=== TRACE ===", fg=typer.colors.MAGENTA, err=True)
        typer.echo(render_trace(gen_res, full=trace_full), err=True)

    typer.secho("=== LOGICAL SQL (LLM Generated) ===", fg=typer.colors.YELLOW, err=True)
    typer.echo(gen_res.logical_sql)
    typer.echo("")
    typer.secho(
        f"=== PHYSICAL SQL (Expanded: models={', '.join(gen_res.models_used)}, "
        f"joins={gen_res.joins_emitted}, pruned={gen_res.joins_pruned}) ===",
        fg=typer.colors.GREEN,
        err=True,
    )
    typer.echo(gen_res.physical_sql)


# ── helpers ───────────────────────────────────────────────────────────────────



def _run_fold(
    *,
    ddl: Path | None,
    dsn: str | None,
    schema: str,
    coverage: float,
    min_gain: int,
    max_cost: float,
    llm: bool,
    max_models: int | None,
    max_hops: int,
    field_budget: int,
) -> FoldResult:
    physical = _load_schema(ddl=ddl, dsn=dsn, schema=schema)
    return fold(
        physical,
        selector=_build_selector(llm),
        policy=SelectionPolicy(
            coverage_target=coverage,
            min_gain=min_gain,
            max_fields_per_table=max_cost,
            max_areas=max_models,
        ),
        max_hops=max_hops,
        field_budget=field_budget,
    )


def _build_selector(llm: bool):
    """Greedy unless the caller asked for a completion in the loop."""
    if not llm:
        return None

    from tablefold.choose.select import LLMSelector
    from tablefold.report.llm import LLMUnavailable, anthropic_completer

    try:
        return LLMSelector(anthropic_completer())
    except LLMUnavailable as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


def _load_schema(
    *,
    ddl: Path | None,
    dsn: str | None,
    schema: str,
    mssql: bool = False,
) -> PhysicalSchema:
    if mssql:
        return _load_mssql(schema)[0]

    if ddl is None and dsn is None:
        typer.secho(
            "error: pass --ddl, --dsn or --mssql", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2)
    if ddl is not None and dsn is not None:
        typer.secho(
            "error: pass only one of --ddl / --dsn", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2)

    if ddl is not None:
        if not ddl.exists():
            typer.secho(f"error: no such file: {ddl}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        return DDLIntrospector.from_path(ddl).introspect()

    from tablefold.read.postgres import PostgresIntrospector

    assert dsn is not None
    return PostgresIntrospector(dsn, schema=schema).introspect()


MSSQL_DEFAULT_SCHEMA = "dbo"


def _load_mssql(schema: str):
    """스키마와, 데이터 검증에 쓸 커서를 함께 돌려준다.

    커서를 밖으로 내보내는 이유는 관계 복구가 **같은 접속** 을 써야 하기 때문이다.
    스키마를 읽고 접속을 닫아 버리면 위반율을 잴 방법이 없다.
    """
    from tablefold.read.mssql import (
        MSSQLIntrospector,
        MSSQLUnavailable,
        connect_from_env,
    )

    # ``--schema`` 의 기본값은 PostgreSQL 의 ``public`` 이다. SQL Server 에는 그런
    # 스키마가 없어서 표를 0개 읽고, 빈 레이어로 조용히 진행된다.
    if schema == "public":
        schema = MSSQL_DEFAULT_SCHEMA

    try:
        connect = connect_from_env()
        physical = MSSQLIntrospector(connect, schema=schema).introspect()
    except MSSQLUnavailable as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        typer.secho(
            f"error: SQL Server 에 접속하지 못했다: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2) from exc

    if not physical.tables:
        typer.secho(
            f"error: 스키마 '{schema}' 에 표가 없다. --schema 를 확인하라.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    return physical, connect


def _load_and_validate(schema: str, *, recover: bool) -> tuple[PhysicalSchema, str]:
    """SQL Server 에서 읽고, 복구한 관계를 **데이터로** 검증해 붙인다.

    이것이 실측(문서 5장)이 나온 경로다. 스키마만 보는 복구는 "가능한" 관계를 전부
    만들지만, 여기서는 참조 대상에 없는 값의 비율을 세어 임계값을 넘는 후보를
    버린다 — 그래서 DDL 만 줄 때보다 모델이 적고 정확하다.
    """
    from tablefold.relate.validate import recover_with_data

    physical, connect = _load_mssql(schema)
    declared = len(physical.foreign_keys)
    if not recover:
        return physical, f"foreign keys {declared} declared"

    conn = connect()
    try:
        physical, recovered = recover_with_data(physical, conn.cursor())
    finally:
        conn.close()
    return physical, (
        f"foreign keys {declared} declared + {len(recovered)} recovered "
        "(validated against the data)"
    )


def _render(result: FoldResult, output_format: str) -> str:
    renderers = {
        "yaml": lambda: emit.to_yaml(result.layer),
        "json": lambda: emit.to_json(result.layer),
        "text": lambda: emit.render_text(result.layer),
        "report": lambda: emit.render_report(result.layer),
    }
    renderer = renderers.get(output_format.lower())
    if renderer is None:
        typer.secho(
            f"error: unknown format '{output_format}'; "
            f"choose from {', '.join(renderers)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    return renderer()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
