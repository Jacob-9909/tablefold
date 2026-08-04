"""Introspect a live PostgreSQL database.

Requires the ``postgres`` extra (``pip install 'tablefold[postgres]'``).

Row counts come from ``pg_class.reltuples``, the planner's estimate. It is
approximate and stale between ``ANALYZE`` runs, which is fine — it feeds one
weighted term in fact scoring, and counting 50 tables exactly would cost a full
scan each for no change in the outcome.
"""

from __future__ import annotations

from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable

_COLUMNS_SQL = """
SELECT
    c.relname                AS table_name,
    a.attname                AS column_name,
    format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull         AS is_nullable,
    col_description(c.oid, a.attnum)     AS column_comment,
    a.attnum                 AS position
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = %(schema)s
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_TABLES_SQL = """
SELECT
    c.relname                AS table_name,
    obj_description(c.oid)   AS table_comment,
    GREATEST(c.reltuples, 0)::bigint AS row_estimate
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s
  AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.relname
"""

_PRIMARY_KEYS_SQL = """
SELECT
    c.relname AS table_name,
    a.attname AS column_name,
    array_position(i.indkey::int[], a.attnum) AS position
FROM pg_index i
JOIN pg_class c   ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
WHERE i.indisprimary AND n.nspname = %(schema)s
ORDER BY c.relname, position
"""

_FOREIGN_KEYS_SQL = """
SELECT
    con.conname                AS constraint_name,
    src.relname                AS from_table,
    src_col.attname            AS from_column,
    tgt.relname                AS to_table,
    tgt_col.attname            AS to_column,
    ordinality                 AS position
FROM pg_constraint con
JOIN pg_class src        ON src.oid = con.conrelid
JOIN pg_namespace n      ON n.oid = src.connamespace
JOIN pg_class tgt        ON tgt.oid = con.confrelid
JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY
     AS keys(src_attnum, tgt_attnum, ordinality) ON TRUE
JOIN pg_attribute src_col ON src_col.attrelid = con.conrelid
                         AND src_col.attnum = keys.src_attnum
JOIN pg_attribute tgt_col ON tgt_col.attrelid = con.confrelid
                         AND tgt_col.attnum = keys.tgt_attnum
WHERE con.contype = 'f' AND n.nspname = %(schema)s
ORDER BY con.conname, ordinality
"""


class PostgresIntrospector:
    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        self._dsn = dsn
        self._schema = schema

    def introspect(self) -> PhysicalSchema:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "PostgreSQL introspection needs the 'postgres' extra: "
                "pip install 'tablefold[postgres]'"
            ) from exc

        params = {"schema": self._schema}
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(_TABLES_SQL, params)
            table_rows = cur.fetchall()
            cur.execute(_COLUMNS_SQL, params)
            column_rows = cur.fetchall()
            cur.execute(_PRIMARY_KEYS_SQL, params)
            pk_rows = cur.fetchall()
            cur.execute(_FOREIGN_KEYS_SQL, params)
            fk_rows = cur.fetchall()

        return _assemble(self._schema, table_rows, column_rows, pk_rows, fk_rows)


def _assemble(
    schema_name: str,
    table_rows: list[tuple],
    column_rows: list[tuple],
    pk_rows: list[tuple],
    fk_rows: list[tuple],
) -> PhysicalSchema:
    columns: dict[str, list[PhysicalColumn]] = {}
    for table_name, column_name, data_type, is_nullable, comment, _ in column_rows:
        columns.setdefault(table_name, []).append(
            PhysicalColumn(
                name=column_name,
                type=data_type,
                nullable=bool(is_nullable),
                comment=comment,
            )
        )

    primary_keys: dict[str, list[str]] = {}
    for table_name, column_name, _ in pk_rows:
        primary_keys.setdefault(table_name, []).append(column_name)

    tables = tuple(
        PhysicalTable(
            name=table_name,
            columns=tuple(columns.get(table_name, ())),
            primary_key=tuple(primary_keys.get(table_name, ())),
            schema=schema_name,
            comment=comment,
            row_estimate=int(row_estimate) if row_estimate is not None else None,
        )
        for table_name, comment, row_estimate in table_rows
        if columns.get(table_name)
    )

    grouped: dict[str, dict] = {}
    for name, from_table, from_column, to_table, to_column, _ in fk_rows:
        entry = grouped.setdefault(
            name,
            {
                "from_table": from_table,
                "to_table": to_table,
                "from_columns": [],
                "to_columns": [],
            },
        )
        entry["from_columns"].append(from_column)
        entry["to_columns"].append(to_column)

    foreign_keys = tuple(
        ForeignKey(
            name=name,
            from_table=entry["from_table"],
            from_columns=tuple(entry["from_columns"]),
            to_table=entry["to_table"],
            to_columns=tuple(entry["to_columns"]),
        )
        for name, entry in grouped.items()
    )

    return PhysicalSchema(tables=tables, foreign_keys=foreign_keys)
