"""Intermediate representation for tablefold.

Two halves:

* **Physical** — what introspection found in the database. A faithful,
  lossless picture of tables, columns, keys, and foreign keys.
* **Logical** — what the folding engine produced. A small set of wide models,
  each anchored to one base table, whose fields carry a *provenance* describing
  exactly how to recover them from the physical schema.

Every type here is frozen. Passes build new objects rather than mutating, so a
pipeline stage can always be re-run against its input and compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

# ── Physical layer ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PhysicalColumn:
    name: str
    type: str
    nullable: bool = True
    comment: str | None = None

    @property
    def is_numeric(self) -> bool:
        return _norm_type(self.type) in _NUMERIC_TYPES

    @property
    def is_temporal(self) -> bool:
        return _norm_type(self.type) in _TEMPORAL_TYPES

    @property
    def is_textual(self) -> bool:
        return _norm_type(self.type) in _TEXT_TYPES


@dataclass(frozen=True)
class PhysicalTable:
    name: str
    columns: tuple[PhysicalColumn, ...]
    primary_key: tuple[str, ...] = ()
    schema: str | None = None
    comment: str | None = None
    row_estimate: int | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def column(self, name: str) -> PhysicalColumn | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


@dataclass(frozen=True)
class ForeignKey:
    """A directed edge: ``from_table.from_columns`` references ``to_table.to_columns``.

    ``inferred`` marks edges recovered by name/type matching rather than read
    from a declared constraint. Backup dumps and warehouse landing zones
    routinely ship without constraints, so inference is a first-class source
    rather than a fallback hack — but callers can filter on the flag when they
    only trust declared structure.
    """

    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    name: str | None = None
    inferred: bool = False
    confidence: float = 1.0


@dataclass(frozen=True)
class PhysicalSchema:
    tables: tuple[PhysicalTable, ...]
    foreign_keys: tuple[ForeignKey, ...] = ()

    def table(self, name: str) -> PhysicalTable | None:
        lowered = name.lower()
        return next((t for t in self.tables if t.name.lower() == lowered), None)

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tables)

    def with_foreign_keys(self, fks: tuple[ForeignKey, ...]) -> PhysicalSchema:
        return replace(self, foreign_keys=fks)


# ── Logical layer ─────────────────────────────────────────────────────────────


class Cardinality(StrEnum):
    """Direction of a join step, seen from the model's base table."""

    MANY_TO_ONE = "many_to_one"
    """Following an outgoing FK. Safe to inline — at most one row matches."""

    ONE_TO_MANY = "one_to_many"
    """Following an FK backwards. Fans out; must be aggregated, never inlined."""


@dataclass(frozen=True)
class JoinStep:
    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    cardinality: Cardinality


class FieldKind(StrEnum):
    BASE = "base"
    """A column of the model's own base table."""

    JOINED = "joined"
    """A column pulled in across one or more many-to-one steps."""

    AGGREGATED = "aggregated"
    """A one-to-many child column collapsed by an aggregate function."""


@dataclass(frozen=True)
class FieldSource:
    kind: FieldKind
    table: str
    column: str
    path: tuple[JoinStep, ...] = ()
    aggregate: str | None = None

    @property
    def hops(self) -> int:
        return len(self.path)


@dataclass(frozen=True)
class LogicalField:
    name: str
    type: str
    source: FieldSource
    description: str | None = None


@dataclass(frozen=True)
class LogicalModel:
    """One wide model. Its grain is exactly one row per ``base_table`` row."""

    name: str
    base_table: str
    fields: tuple[LogicalField, ...]
    absorbed_tables: tuple[str, ...] = ()
    description: str | None = None

    def field(self, name: str) -> LogicalField | None:
        lowered = name.lower()
        return next((f for f in self.fields if f.name.lower() == lowered), None)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)


@dataclass(frozen=True)
class LogicalLayer:
    models: tuple[LogicalModel, ...]
    source_table_count: int = 0
    covered_table_count: int = 0
    stop_reason: str | None = None
    """Why anchor selection stopped, carried through so a saved layer explains
    its own size. A layer that stopped short of its coverage target is a
    reportable fact, not a defect to be discovered later."""

    selector: str | None = None
    """Who chose the anchors. A layer whose anchors came from a language model
    should not be indistinguishable from one that did not — a reviewer deciding
    how hard to check it needs to know which they are holding."""

    notes: tuple[str, ...] = field(default_factory=tuple)

    def model(self, name: str) -> LogicalModel | None:
        lowered = name.lower()
        return next((m for m in self.models if m.name.lower() == lowered), None)

    @property
    def compression_ratio(self) -> float:
        """Physical tables per logical model. The headline number."""
        if not self.models:
            return 0.0
        return self.source_table_count / len(self.models)

    @property
    def coverage(self) -> float:
        """Fraction of physical tables that landed in at least one model."""
        if not self.source_table_count:
            return 0.0
        return self.covered_table_count / self.source_table_count


# ── Type vocabulary ───────────────────────────────────────────────────────────

_NUMERIC_TYPES = frozenset(
    {
        "smallint",
        "integer",
        "bigint",
        "decimal",
        "numeric",
        "real",
        "double",
        "float",
        "money",
        "int",
        "int2",
        "int4",
        "int8",
        "serial",
        "bigserial",
    }
)

_TEMPORAL_TYPES = frozenset(
    {
        "date",
        "time",
        "timestamp",
        "timestamptz",
        "datetime",
        "interval",
    }
)

_TEXT_TYPES = frozenset(
    {
        "char",
        "varchar",
        "text",
        "citext",
        "uuid",
        "name",
    }
)


def _norm_type(raw: str) -> str:
    """Reduce a declared SQL type to a bare lowercase base name.

    ``NUMERIC(10, 2)`` -> ``numeric``; ``TIMESTAMP WITH TIME ZONE`` ->
    ``timestamptz``; ``CHARACTER VARYING(255)`` -> ``varchar``.
    """
    t = raw.strip().lower()
    if "(" in t:
        t = t.split("(", 1)[0].strip()
    if "with time zone" in t:
        return "timestamptz" if t.startswith("timestamp") else "time"
    t = t.replace("without time zone", "").strip()
    aliases = {
        "character varying": "varchar",
        "character": "char",
        "double precision": "double",
        "timestamp with time zone": "timestamptz",
    }
    t = aliases.get(t, t)
    return t.split()[0] if t else t
