"""Command line interface."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from tablefold import emit
from tablefold.expand import ExpansionError, expand
from tablefold.introspect.ddl import DDLIntrospector
from tablefold.ir import PhysicalSchema
from tablefold.pipeline import FoldResult, fold

app = typer.Typer(
    add_completion=False,
    help="Fold a wide physical schema into a few wide logical models.",
)

DdlOption = Annotated[
    Path | None, typer.Option("--ddl", help="Path to a DDL script to read.")
]
DsnOption = Annotated[
    str | None,
    typer.Option(
        "--dsn", help="PostgreSQL connection string (needs the postgres extra)."
    ),
]
SchemaOption = Annotated[str, typer.Option("--schema", help="Database schema name.")]
ModelsOption = Annotated[
    int, typer.Option("--models", "-n", min=1, help="Maximum number of logical models.")
]
HopsOption = Annotated[
    int,
    typer.Option("--max-hops", min=1, help="How far to follow foreign keys forwards."),
]
FieldsOption = Annotated[
    int, typer.Option("--max-fields", min=1, help="Field cap per model.")
]


@app.command("fold")
def fold_command(
    ddl: DdlOption = None,
    dsn: DsnOption = None,
    schema: SchemaOption = "public",
    models: ModelsOption = 4,
    max_hops: HopsOption = 3,
    max_fields: FieldsOption = 64,
    output: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write the layer to this path.")
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="yaml | json | text | report")
    ] = "report",
) -> None:
    """Read a schema, fold it, and write the logical layer."""
    result = _run_fold(
        ddl=ddl,
        dsn=dsn,
        schema=schema,
        models=models,
        max_hops=max_hops,
        max_fields=max_fields,
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
    models: ModelsOption = 4,
    max_hops: HopsOption = 3,
    max_fields: FieldsOption = 64,
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
        models=models,
        max_hops=max_hops,
        max_fields=max_fields,
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
    models: ModelsOption = 4,
    max_hops: HopsOption = 3,
    max_fields: FieldsOption = 64,
) -> None:
    """Print the compact schema text intended for a prompt."""
    result = _run_fold(
        ddl=ddl,
        dsn=dsn,
        schema=schema,
        models=models,
        max_hops=max_hops,
        max_fields=max_fields,
    )
    text = emit.render_text(result.layer)
    typer.secho(
        f"-- {len(text)} chars, ~{len(text) // 4} tokens",
        fg=typer.colors.CYAN,
        err=True,
    )
    typer.echo(text)


# ── helpers ───────────────────────────────────────────────────────────────────


def _run_fold(
    *,
    ddl: Path | None,
    dsn: str | None,
    schema: str,
    models: int,
    max_hops: int,
    max_fields: int,
) -> FoldResult:
    physical = _load_schema(ddl=ddl, dsn=dsn, schema=schema)
    return fold(
        physical,
        target_models=models,
        max_hops=max_hops,
        max_fields=max_fields,
    )


def _load_schema(*, ddl: Path | None, dsn: str | None, schema: str) -> PhysicalSchema:
    if ddl is None and dsn is None:
        typer.secho("error: pass --ddl or --dsn", fg=typer.colors.RED, err=True)
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

    from tablefold.introspect.postgres import PostgresIntrospector

    assert dsn is not None
    return PostgresIntrospector(dsn, schema=schema).introspect()


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
