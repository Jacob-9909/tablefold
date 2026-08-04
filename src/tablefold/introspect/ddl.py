"""Introspect a physical schema from a DDL script.

Reads ``CREATE TABLE`` (inline ``PRIMARY KEY`` / ``REFERENCES`` / table-level
constraints) and ``ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY``, which is
how ``pg_dump`` emits foreign keys. Anything else in the script is skipped
rather than treated as an error, so a full dump can be pointed at directly.
"""

from __future__ import annotations

from pathlib import Path

import sqlglot
from sqlglot import exp

from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable


class DDLIntrospector:
    def __init__(self, ddl: str, *, dialect: str = "postgres") -> None:
        self._ddl = ddl
        self._dialect = dialect

    @classmethod
    def from_path(
        cls, path: str | Path, *, dialect: str = "postgres"
    ) -> DDLIntrospector:
        return cls(Path(path).read_text(encoding="utf-8"), dialect=dialect)

    def introspect(self) -> PhysicalSchema:
        statements = [
            s
            for s in sqlglot.parse(self._ddl, read=self._dialect, error_level=None)
            if s is not None
        ]

        tables: list[PhysicalTable] = []
        foreign_keys: list[ForeignKey] = []

        for stmt in statements:
            if (
                isinstance(stmt, exp.Create)
                and (stmt.args.get("kind") or "").upper() == "TABLE"
            ):
                table, fks = self._read_create(stmt)
                if table is not None:
                    tables.append(table)
                    foreign_keys.extend(fks)
            elif isinstance(stmt, exp.Alter):
                foreign_keys.extend(self._read_alter(stmt))

        known = {t.name.lower() for t in tables}
        resolved = tuple(
            fk
            for fk in foreign_keys
            if fk.from_table.lower() in known and fk.to_table.lower() in known
        )
        return PhysicalSchema(tables=tuple(tables), foreign_keys=resolved)

    # ── CREATE TABLE ─────────────────────────────────────────────────────────

    def _read_create(
        self, stmt: exp.Create
    ) -> tuple[PhysicalTable | None, list[ForeignKey]]:
        target = stmt.this
        if not isinstance(target, exp.Schema) or not isinstance(target.this, exp.Table):
            return None, []

        table_name = target.this.name
        schema_name = target.this.db or None
        if not table_name:
            return None, []

        columns: list[PhysicalColumn] = []
        primary_key: tuple[str, ...] = ()
        fks: list[ForeignKey] = []

        for item in target.expressions:
            if isinstance(item, exp.ColumnDef):
                column, col_pk, col_fk = self._read_column_def(item, table_name)
                columns.append(column)
                if col_pk:
                    primary_key = (*primary_key, column.name)
                if col_fk is not None:
                    fks.append(col_fk)
            elif isinstance(item, exp.PrimaryKey):
                primary_key = tuple(_identifier_names(item.expressions))
            elif isinstance(item, exp.ForeignKey):
                fk = self._read_table_foreign_key(item, table_name)
                if fk is not None:
                    fks.append(fk)
            elif isinstance(item, exp.Constraint):
                for inner in item.args.get("expressions") or []:
                    if isinstance(inner, exp.PrimaryKey):
                        primary_key = tuple(_identifier_names(inner.expressions))
                    elif isinstance(inner, exp.ForeignKey):
                        fk = self._read_table_foreign_key(
                            inner,
                            table_name,
                            name=item.this.name if item.this else None,
                        )
                        if fk is not None:
                            fks.append(fk)

        if not columns:
            return None, []

        return (
            PhysicalTable(
                name=table_name,
                columns=tuple(columns),
                primary_key=primary_key,
                schema=schema_name,
            ),
            fks,
        )

    def _read_column_def(
        self, node: exp.ColumnDef, table_name: str
    ) -> tuple[PhysicalColumn, bool, ForeignKey | None]:
        name = node.name
        kind = node.args.get("kind")
        type_name = kind.sql(dialect=self._dialect) if kind is not None else "unknown"

        nullable = True
        is_pk = False
        fk: ForeignKey | None = None

        for constraint in node.constraints or []:
            body = constraint.args.get("kind")
            if isinstance(body, exp.PrimaryKeyColumnConstraint):
                is_pk = True
                nullable = False
            elif isinstance(body, exp.NotNullColumnConstraint):
                nullable = body.args.get("allow_null") is True
            elif isinstance(body, exp.Reference):
                fk = self._read_column_reference(body, table_name, name)

        return PhysicalColumn(name=name, type=type_name, nullable=nullable), is_pk, fk

    def _read_column_reference(
        self, node: exp.Reference, from_table: str, from_column: str
    ) -> ForeignKey | None:
        target = node.this
        if isinstance(target, exp.Schema):
            to_table_node = target.this
            to_columns = tuple(_identifier_names(target.expressions))
        elif isinstance(target, exp.Table):
            to_table_node = target
            to_columns = ()
        else:
            return None

        if not isinstance(to_table_node, exp.Table) or not to_table_node.name:
            return None

        return ForeignKey(
            from_table=from_table,
            from_columns=(from_column,),
            to_table=to_table_node.name,
            to_columns=to_columns,
        )

    def _read_table_foreign_key(
        self, node: exp.ForeignKey, from_table: str, *, name: str | None = None
    ) -> ForeignKey | None:
        from_columns = tuple(_identifier_names(node.expressions))
        reference = node.args.get("reference")
        if reference is None or not from_columns:
            return None

        resolved = self._read_column_reference(reference, from_table, from_columns[0])
        if resolved is None:
            return None
        return ForeignKey(
            from_table=from_table,
            from_columns=from_columns,
            to_table=resolved.to_table,
            to_columns=resolved.to_columns,
            name=name,
        )

    # ── ALTER TABLE ──────────────────────────────────────────────────────────

    def _read_alter(self, stmt: exp.Alter) -> list[ForeignKey]:
        target = stmt.this
        if not isinstance(target, exp.Table) or not target.name:
            return []

        found: list[ForeignKey] = []
        for action in stmt.args.get("actions") or []:
            for node in _walk_constraints(action):
                if isinstance(node, exp.ForeignKey):
                    fk = self._read_table_foreign_key(node, target.name)
                    if fk is not None:
                        found.append(fk)
        return found


def _walk_constraints(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Constraint):
        return list(node.args.get("expressions") or [])
    if isinstance(node, exp.AddConstraint):
        collected: list[exp.Expression] = []
        for inner in node.args.get("expressions") or []:
            collected.extend(_walk_constraints(inner))
        return collected or [node]
    return [node]


def _identifier_names(nodes) -> list[str]:
    names: list[str] = []
    for node in nodes or []:
        if isinstance(node, exp.Identifier | exp.Column):
            names.append(node.name)
        elif isinstance(node, exp.Ordered):
            names.extend(_identifier_names([node.this]))
        elif hasattr(node, "name") and node.name:
            names.append(node.name)
    return names
