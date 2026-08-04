"""The foreign-key graph, and recovery of edges the database never declared.

Everything downstream — fact detection, clustering, field promotion, join
expansion — is a walk over this graph. Edges are directed: an edge points from
the table holding the key to the table holding the referenced row, so following
an edge forwards is always many-to-one and following it backwards is always
one-to-many.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from tablefold.schema.ir import Cardinality, ForeignKey, JoinStep, PhysicalSchema

# Suffixes that mark a column as a reference to another table's key.
_KEY_SUFFIXES = ("_id", "_code", "_key", "_no", "_fk")

# Column names too generic to infer a target from.
_UNINFERABLE = frozenset({"id", "parent_id", "actor_id", "entity_id", "user_id"})


@dataclass(frozen=True)
class SchemaGraph:
    schema: PhysicalSchema
    _out: dict[str, tuple[ForeignKey, ...]]
    _in: dict[str, tuple[ForeignKey, ...]]

    @classmethod
    def build(cls, schema: PhysicalSchema) -> SchemaGraph:
        out: dict[str, list[ForeignKey]] = defaultdict(list)
        inn: dict[str, list[ForeignKey]] = defaultdict(list)
        for fk in schema.foreign_keys:
            out[fk.from_table.lower()].append(fk)
            inn[fk.to_table.lower()].append(fk)
        return cls(
            schema=schema,
            _out={k: tuple(v) for k, v in out.items()},
            _in={k: tuple(v) for k, v in inn.items()},
        )

    # ── adjacency ────────────────────────────────────────────────────────────

    def outgoing(self, table: str) -> tuple[ForeignKey, ...]:
        """FKs this table holds. Following one is many-to-one."""
        return self._out.get(table.lower(), ())

    def incoming(self, table: str) -> tuple[ForeignKey, ...]:
        """FKs pointing at this table. Following one backwards is one-to-many."""
        return self._in.get(table.lower(), ())

    def out_degree(self, table: str) -> int:
        return len(self.outgoing(table))

    def in_degree(self, table: str) -> int:
        return len(self.incoming(table))

    def neighbours(self, table: str) -> set[str]:
        """Adjacent tables, ignoring direction, excluding self-references."""
        lowered = table.lower()
        found = {fk.to_table.lower() for fk in self.outgoing(table)}
        found |= {fk.from_table.lower() for fk in self.incoming(table)}
        return found - {lowered}

    # ── traversal ────────────────────────────────────────────────────────────

    def components(self) -> tuple[frozenset[str], ...]:
        """Connected components over the undirected projection of the graph."""
        unseen = {t.name.lower() for t in self.schema.tables}
        found: list[frozenset[str]] = []
        while unseen:
            root = min(unseen)
            group: set[str] = set()
            queue = deque([root])
            while queue:
                current = queue.popleft()
                if current in group:
                    continue
                group.add(current)
                unseen.discard(current)
                queue.extend(self.neighbours(current) - group)
            found.append(frozenset(group))
        return tuple(sorted(found, key=lambda g: (-len(g), min(g))))

    def shortest_path(
        self, source: str, target: str, *, max_hops: int = 6
    ) -> int | None:
        """Undirected hop count between two tables, or ``None`` if unreachable."""
        src, dst = source.lower(), target.lower()
        if src == dst:
            return 0
        seen = {src}
        queue = deque([(src, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= max_hops:
                continue
            for neighbour in self.neighbours(current):
                if neighbour == dst:
                    return depth + 1
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append((neighbour, depth + 1))
        return None

    def walk_many_to_one(
        self, source: str, *, max_hops: int
    ) -> tuple[tuple[str, tuple[JoinStep, ...]], ...]:
        """Every table reachable from *source* by following outgoing FKs only.

        Returns ``(table, path)`` pairs, shortest path first. Because every step
        is many-to-one, joining along any of these paths preserves the source
        table's grain — that is the whole reason the fold is safe.
        """
        results: list[tuple[str, tuple[JoinStep, ...]]] = []
        seen = {source.lower()}
        queue: deque[tuple[str, tuple[JoinStep, ...]]] = deque([(source, ())])

        while queue:
            current, path = queue.popleft()
            if len(path) >= max_hops:
                continue
            for fk in self.outgoing(current):
                target = fk.to_table.lower()
                if target in seen:
                    continue
                step = JoinStep(
                    from_table=fk.from_table,
                    from_columns=fk.from_columns,
                    to_table=fk.to_table,
                    to_columns=fk.to_columns or self._key_of(fk.to_table),
                    cardinality=Cardinality.MANY_TO_ONE,
                )
                extended = (*path, step)
                seen.add(target)
                results.append((fk.to_table, extended))
                queue.append((fk.to_table, extended))

        return tuple(results)

    def children(self, table: str) -> tuple[tuple[str, JoinStep], ...]:
        """Tables that reference *table*. One step backwards — one-to-many."""
        found: list[tuple[str, JoinStep]] = []
        for fk in self.incoming(table):
            if fk.from_table.lower() == table.lower():
                continue
            step = JoinStep(
                from_table=fk.to_table,
                from_columns=fk.to_columns or self._key_of(fk.to_table),
                to_table=fk.from_table,
                to_columns=fk.from_columns,
                cardinality=Cardinality.ONE_TO_MANY,
            )
            found.append((fk.from_table, step))
        return tuple(found)

    def _key_of(self, table: str) -> tuple[str, ...]:
        found = self.schema.table(table)
        return found.primary_key if found and found.primary_key else ("id",)


# ── inference ─────────────────────────────────────────────────────────────────


def infer_foreign_keys(schema: PhysicalSchema) -> tuple[ForeignKey, ...]:
    """Recover undeclared references by matching column names against keys.

    A column qualifies when its name strips to a known table (``customer_id`` ->
    ``customers``) and its type matches that table's single-column primary key.
    Edges already declared are never duplicated.

    This exists because backup dumps and warehouse landing zones routinely
    arrive with every constraint stripped, leaving a schema whose real structure
    is present only in naming convention.
    """
    by_key: dict[tuple[str, str], str] = {}
    for table in schema.tables:
        if len(table.primary_key) != 1:
            continue
        pk_column = table.column(table.primary_key[0])
        if pk_column is None:
            continue
        for alias in _name_aliases(table.name):
            by_key[(alias, _type_class(pk_column.type))] = table.name

    declared = {(fk.from_table.lower(), fk.from_columns) for fk in schema.foreign_keys}

    found: list[ForeignKey] = []
    for table in schema.tables:
        for column in table.columns:
            lowered = column.name.lower()
            if lowered in _UNINFERABLE or lowered in {
                c.lower() for c in table.primary_key
            }:
                continue
            if (table.name.lower(), (column.name,)) in declared:
                continue

            stem = _strip_key_suffix(lowered)
            if stem is None:
                continue

            target = by_key.get((stem, _type_class(column.type)))
            if target is None or target.lower() == table.name.lower():
                continue

            target_table = schema.table(target)
            if target_table is None:
                continue

            found.append(
                ForeignKey(
                    from_table=table.name,
                    from_columns=(column.name,),
                    to_table=target_table.name,
                    to_columns=target_table.primary_key,
                    inferred=True,
                    confidence=0.7,
                )
            )
    return tuple(found)


def _strip_key_suffix(column_name: str) -> str | None:
    for suffix in _KEY_SUFFIXES:
        if column_name.endswith(suffix) and len(column_name) > len(suffix):
            return column_name[: -len(suffix)]
    return None


def _name_aliases(table_name: str) -> set[str]:
    """Singular/plural spellings a referencing column might use."""
    lowered = table_name.lower()
    aliases = {lowered}
    if lowered.endswith("ies"):
        aliases.add(lowered[:-3] + "y")
    if lowered.endswith("ses"):
        aliases.add(lowered[:-2])
    if lowered.endswith("s"):
        aliases.add(lowered[:-1])
    else:
        aliases.add(lowered + "s")
    return aliases


def _type_class(raw: str) -> str:
    """Collapse a declared type to a comparability class.

    Width is deliberately discarded: an ``integer`` FK pointing at a ``bigint``
    key is normal, and refusing to match on that difference would lose most real
    edges.
    """
    from tablefold.schema.ir import _norm_type

    base = _norm_type(raw)
    integers = {
        "smallint",
        "integer",
        "bigint",
        "int",
        "int2",
        "int4",
        "int8",
        "serial",
        "bigserial",
    }
    if base in integers:
        return "int"
    if base in {"char", "varchar", "text", "citext", "name"}:
        return "text"
    return base
