"""외래 키 그래프 구축 및 데이터베이스에 명시되지 않은 엣지 추론/복구.

Fact 탐지, 클러스터링, 필드 승격, 조인 확장 등 하위의 모든 알고리즘은
이 그래프 탐색을 기반으로 작동합니다.
엣지는 방향성을 가집니다: 엣지는 키를 가진 테이블에서 참조 대상 행을 가진
테이블로 향하므로, 엣지를 정방향으로 따라가는 것은 항상 N:1(Many-to-One)이며
역방향으로 따라가는 것은 항상 1:N(One-to-Many)입니다.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from tablefold.schema.ir import (
    Cardinality,
    ForeignKey,
    JoinStep,
    PhysicalSchema,
    _norm_type,
    name_aliases,
)

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
        """이 테이블이 가진 FK 목록. 이 엣지를 따르는 것은 N:1 관계입니다."""
        return self._out.get(table.lower(), ())

    def incoming(self, table: str) -> tuple[ForeignKey, ...]:
        """이 테이블을 가리키는 FK 목록.
        이 엣지를 역방향으로 따르는 것은 1:N 관계입니다.
        """
        return self._in.get(table.lower(), ())

    def out_degree(self, table: str) -> int:
        return len(self.outgoing(table))

    def in_degree(self, table: str) -> int:
        return len(self.incoming(table))

    # ── traversal ────────────────────────────────────────────────────────────

    def walk_many_to_one(
        self, source: str, *, max_hops: int
    ) -> tuple[tuple[str, tuple[JoinStep, ...]], ...]:
        """*source*에서 출발하여 나가는 FK만 따라 도달 가능한 모든 테이블을 반환합니다.

        최단 경로 순으로 ``(table, path)`` 쌍을 반환합니다. 모든 단계가 N:1 관계이므로
        이 경로들을 따라 조인하는 것은 source 테이블의 입도(Grain)를 완벽히 보존합니다.

        방문 여부는 도달한 *테이블* 이 아니라 지나온 *엣지* 로 기록한다. 테이블로
        기록하면 한 테이블이 같은 대상을 두 개의 키로 참조할 때 — ``orders`` 의
        ``buyer_id`` 와 ``seller_id`` 가 모두 ``users`` 를 가리키는 흔한 모양 —
        먼저 도달한 쪽이 대상을 소진해 버려서 두 번째 경로가 통째로 사라진다.
        판매자 정보가 모델에서 조용히 빠지고, 조인은 무조건 구매자 쪽으로 걸린다.

        경로 깊이는 ``max_hops`` 가 묶고, 같은 엣지를 두 번 밟지 않으므로 순환
        외래 키에서도 끝난다.
        """
        results: list[tuple[str, tuple[JoinStep, ...]]] = []
        seen: set[tuple[str, tuple[str, ...], str]] = set()
        queue: deque[tuple[str, tuple[JoinStep, ...]]] = deque([(source, ())])

        while queue:
            current, path = queue.popleft()
            if len(path) >= max_hops:
                continue
            for fk in self.outgoing(current):
                if fk.to_table.lower() == source.lower():
                    continue  # 출발점으로 되돌아오는 경로는 아무것도 더하지 않는다
                edge = (
                    fk.from_table.lower(),
                    tuple(c.lower() for c in fk.from_columns),
                    fk.to_table.lower(),
                )
                if edge in seen:
                    continue
                step = JoinStep(
                    from_table=fk.from_table,
                    from_columns=fk.from_columns,
                    to_table=fk.to_table,
                    to_columns=fk.to_columns or self._key_of(fk.to_table),
                    cardinality=Cardinality.MANY_TO_ONE,
                )
                extended = (*path, step)
                seen.add(edge)
                results.append((fk.to_table, extended))
                queue.append((fk.to_table, extended))

        return tuple(results)

    def children(self, table: str) -> tuple[tuple[str, JoinStep], ...]:
        """*table*을 참조하는 테이블 목록 (자식 테이블). 역방향 1단계(1:N 관계)."""
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
    """컬럼명 규칙과 데이터 타입을 기반으로 누락된 외래 키 관계를 복구/추론합니다."""
    by_key: dict[tuple[str, str], str] = {}
    for table in schema.tables:
        if len(table.primary_key) != 1:
            continue
        pk_column = table.column(table.primary_key[0])
        if pk_column is None:
            continue
        for alias in name_aliases(table.name):
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


def _type_class(raw: str) -> str:
    """Collapse a declared type to a comparability class.

    Width is deliberately discarded: an ``integer`` FK pointing at a ``bigint``
    key is normal, and refusing to match on that difference would lose most real
    edges.
    """
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
