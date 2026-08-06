"""tablefold의 중간 표현(IR; Intermediate Representation).

두 파트로 구성됩니다:

* **Physical (물리 레이어)** — 데이터베이스 탐색/인트로스펙션을 통해 발견한 스키마.
  테이블, 컬럼, 키, 외래 키의 손실 없는 형태입니다.
* **Logical (논리 레이어)** — 폴딩 엔진이 생성한 구조.
  소수의 와이드 모델로 구성되며, 각 모델은 하나의 베이스 테이블에 앵커링되고,
  각 필드는 물리 스키마로부터 이를 재구성하는 출처(provenance) 정보를 담고 있습니다.

이 모듈의 모든 타입은 불변(frozen) 객체입니다. 파이프라인 단계는 객체를 직접
수정하지 않고 새로운 객체를 생성하므로 이전 단계를 재실행하거나 비교할 수 있습니다.
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

    source_sql: str | None = None
    """물리 테이블이 아니라 이 SQL이 이 테이블의 행을 낸다 (가상 테이블).

    쓰임은 둘이다. **가상 앵커** — 여러 팩트가 공유하는 키를 모아 줄 단위를
    정의한다. 실측 스키마에서 ``YYYYMM`` 은 7개 팩트에 있는데 캘린더 차원이
    없어서, 앵커로 삼을 테이블이 존재하지 않았다. **수직 결합** — 정규직/계약직
    급여처럼 구조가 같고 조인되지 않는 표를 한 테이블로 세운다.

    앵커로 쓰려면 키당 **정확히 한 행**이어야 한다. ``UNION ALL`` 이 아니라
    ``UNION`` 이어야 하고, 그렇지 않으면 앵커 자신이 복제되어 입도 보호가 무너진다.
    """

    @property
    def is_virtual(self) -> bool:
        return self.source_sql is not None

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
    """방향성이 있는 엣지: ``from_table.from_columns``가
    ``to_table.to_columns``를 참조합니다.

    ``inferred``는 명시적 제약 조건이 아닌 이름/타입 매칭을 통해
    추론 복구된 엣지를 표시합니다.
    """

    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    name: str | None = None
    inferred: bool = False
    confidence: float = 1.0

    key_expressions: tuple[str, ...] | None = None
    """``from_columns`` 를 참조 대상의 키로 바꾸는 식 (**파생키**). 컬럼당 하나.

    ``F_SALES.YYYYMMDD`` 와 ``V_CALENDAR.YYYYMM`` 은 같은 기간을 뜻하지만 값이
    다르다. ``("substr(YYYYMMDD, 1, 6)",)`` 를 주면 양방향이 모두 성립한다::

        N:1 인라인   ON substr(base.YYYYMMDD, 1, 6) = j.YYYYMM
        1:N 선집계   SELECT substr(YYYYMMDD, 1, 6) AS YYYYMM, SUM(...)
                     GROUP BY substr(YYYYMMDD, 1, 6)

    :attr:`condition` 과 달리 **부모 키를 자식에서 계산할 수 있다.** 그래서
    선집계가 여전히 부모 키당 한 행을 내고, 입도 보호가 무너지지 않는다.
    식은 자식 컬럼만 참조해야 하며 한정자를 붙이지 않는다.
    """

    condition: str | None = None
    """조인 술어를 컬럼 등가 대신 이 SQL로 쓴다 (**비등가**).

    ``from_columns``/``to_columns`` 는 그대로 채워 둔다 — 카디널리티 재검증과
    자식 키 식별이 그것을 읽는다. 이 필드는 ``ON`` 절만 덮는다.

    별칭은 확장이 경로에서 만들어 내므로 사람이 쓸 수 없다. 플레이스홀더로 적으면
    확장이 바인딩한다. ``{L}`` 은 **키를 가진 쪽**(다), ``{R}`` 은 **참조되는
    쪽**(일)이다 — 탐색 방향이 뒤집혀도(:meth:`SchemaGraph.children`) 이 대응은
    변하지 않으므로 술어를 한 번만 적으면 된다::

        "{L}.EMP_NO = {R}.EMP_NO AND {L}.WORK_YMD "
        "BETWEEN {R}.VALID_FROM AND {R}.VALID_TO"          # 비등가(SCD2)

    **N:1 인라인 전용이다.** 역방향(1:N)에서는 자식에서 부모 키를 계산할 수 없어
    선집계가 부모 입도를 못 맞춘다. 확장이 거부한다 — 파생키라면
    :attr:`key_expressions` 를 쓴다.
    """


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
    """모델의 베이스 테이블 기준 조인 단계의 방향성(입도관계)."""

    MANY_TO_ONE = "many_to_one"
    """나가는 FK를 따라가는 관계. 최대 1개 행만 매칭되어 인라인(직접 조인)이 안전함."""

    ONE_TO_MANY = "one_to_many"
    """FK를 역방향으로 따라가는 관계.
    행 수가 늘어나므로 인라인하지 않고 반드시 집계(Aggregate)해야 함.
    """


@dataclass(frozen=True)
class JoinStep:
    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    cardinality: Cardinality

    key_expressions: tuple[str, ...] | None = None
    """:attr:`ForeignKey.key_expressions`. **다(many) 쪽 컬럼** 기준으로 적힌다."""

    condition: str | None = None
    """:attr:`ForeignKey.condition` 을 그대로 나른다. ``{L}``/``{R}`` 플레이스홀더."""


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

    filter_only: bool = False
    """WHERE 절에서만 쓸 수 있고 SELECT 에는 쓸 수 없는 필드.

    집계된 자식의 원본 컬럼을 위해 존재한다. ``F_SALES`` 를 조직 단위로 접으면
    ``SUM(SALES_AMT)`` 은 남지만 ``YYYYMMDD`` 는 집계 과정에서 사라진다. 그러면
    "이번 달 매출"을 물을 방법이 없어진다 — 사전집계된 값에는 기간을 걸 수단이
    없기 때문이다.

    이 필드는 그 기간 조건을 받는 자리다. 값으로 꺼낼 수는 없고(자식 행마다
    다르므로 앵커 한 행에 대응하는 값이 없다), :mod:`tablefold.rewrite.expand`
    가 조건을 해당 집계 서브쿼리 *안* 으로 밀어 넣는다.
    """


@dataclass(frozen=True)
class LogicalModel:
    """하나의 와이드 모델. 입도(Grain)는 정확히 ``base_table``의 행당 1개 행입니다."""

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
    source_column_count: int = 0
    covered_table_count: int = 0
    stop_reason: str | None = None
    """앵커 선택이 중단된 이유.
    저장된 레이어가 자체 크기의 원인을 설명할 수 있도록 보존됩니다.
    """

    selector: str | None = None
    """앵커를 선택한 주체.
    LLM에 의해 선택된 레이어인지 알고리즘으로 선택된 레이어인지 구별할 수 있게 합니다.
    """

    notes: tuple[str, ...] = field(default_factory=tuple)

    def model(self, name: str) -> LogicalModel | None:
        lowered = name.lower()
        return next((m for m in self.models if m.name.lower() == lowered), None)

    @property
    def field_count(self) -> int:
        return sum(len(m.fields) for m in self.models)

    @property
    def compression_ratio(self) -> float:
        """논리 필드 1개당 물리 컬럼 수.

        이전에는 모델당 테이블 수였는데, 그것은 압축이 아니라 집약을 재는 값이었고
        정확히 잘못된 것을 보상했다. retail 픽스처를 스키마의 30%만 닿는 모델 1개로
        접으면 53:1이 나와서, 실제로 스키마를 커버한 레이어보다 4배 좋은 점수를 받았다.

        컬럼/필드는 읽는 쪽이 실제로 지불하는 값이다. 테이블을 버려서 개선할 수 없다 —
        버려진 테이블의 컬럼은 분자에서도 함께 빠지기 때문이다. ``coverage``와 나란히
        읽어야 한다. 이 값은 레이어가 얼마나 조밀한지를, 저 값은 스키마의 얼마를
        대변하는지를 말한다. 둘 중 하나만으로는 의미가 없다.
        """
        if not self.field_count:
            return 0.0
        return self.source_column_count / self.field_count

    @property
    def coverage(self) -> float:
        """최소 하나 이상의 모델에 포함된 물리 테이블의 비율(커버리지)."""
        if not self.source_table_count:
            return 0.0
        return self.covered_table_count / self.source_table_count


# ── Naming ────────────────────────────────────────────────────────────────────
#
# 테이블 이름의 단복수 변환은 세 곳에서 필요하다 — ``compose``의 필드 접두사,
# ``graph``의 외래 키 추론에서 쓰는 어간, 집계 필드 접두사. 이것들이 각각
# 다른 규칙을 가진 private 헬퍼 세 벌로 존재했고, 그래서 ``addresses``가 한쪽에서는
# ``addresse``로 다른 쪽에서는 ``address``로 갈렸다. 한 벌, 한 가지 답.


# 단수와 복수가 같은 낱말. 규칙으로는 잡히지 않는다 — `series`는 `ies` 규칙에
# 걸려 `sery`가 되고, `news`는 뒤의 `s`가 떨어져 `new`가 된다.
_UNCHANGED_PLURALS = frozenset(
    {"series", "species", "news", "data", "metadata", "media", "sales", "means"}
)


def singular(name: str) -> str:
    lowered = name.lower()
    if lowered in _UNCHANGED_PLURALS:
        return lowered
    # 이름이 접두사를 달고 있어도 어간은 같다: `F_SALES` 의 어간은 `sales` 다.
    tail = lowered.rsplit("_", 1)[-1]
    if tail in _UNCHANGED_PLURALS:
        return lowered

    for plural_suffix, stem_suffix in (
        ("ies", "y"),
        ("sses", "ss"),
        ("shes", "sh"),
        ("ches", "ch"),
        ("xes", "x"),
        ("ses", "s"),
    ):
        if lowered.endswith(plural_suffix):
            return lowered[: -len(plural_suffix)] + stem_suffix
    # `ss`/`us`/`is`로 끝나면 복수형이 아니다: status, address, analysis.
    if lowered.endswith("s") and not lowered.endswith(("ss", "us", "is")):
        return lowered[:-1]
    return lowered


def plural(name: str) -> str:
    lowered = name.lower()
    return lowered if lowered.endswith("s") else f"{lowered}s"


def name_aliases(name: str) -> set[str]:
    """참조하는 컬럼이 *name*을 지칭할 때 쓸 수 있는 철자 형태들."""
    return {name.lower(), singular(name), plural(name)}


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
