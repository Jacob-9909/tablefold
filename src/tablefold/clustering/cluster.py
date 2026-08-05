"""Build the candidate lattice, hand it to a selector, assemble the result.

An anchor is a table whose grain a wide model sits at. Picking them by score
alone fails: in a connected schema the top-scoring facts are all adjacent, so
the top few are several views of the same corner and half the schema goes
uncovered.

Two questions are kept separate on purpose, and this module only answers the
first:

* **Who may anchor?** Every table. A model anchored on a table exposes that
  table's own grain, and nothing about a low fact score makes that impossible.
  Gating the pool on score was measured to strand tables no other anchor could
  reach — ``employees`` scores 0.24 and anchors a four-table model.
* **Who is worth anchoring?** A :class:`~tablefold.select.Selector`. Greedy set
  cover by default; see :mod:`tablefold.select` for why that is a separate
  decision and what else can make it.

Whatever the selector returns is re-measured here. Coverage, membership and the
uncovered list are computed from the graph, never read back from a selector — so
a selector can choose badly but cannot misreport.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.clustering.select import (
    Candidate,
    CandidateLattice,
    GreedySelector,
    SelectionPolicy,
    Selector,
    StopReason,
)
from tablefold.graph.graph import SchemaGraph
from tablefold.presentation.cost import MAX_MODEL_FIELDS, estimate_fields
from tablefold.scoring.classify import TableProfile

__all__ = [
    "Clustering",
    "SelectionPolicy",
    "StopReason",
    "SubjectArea",
    "build_lattice",
    "cluster",
    "reachable_tables",
]


@dataclass(frozen=True)
class SubjectArea:
    """One anchor and the tables it can absorb.

    ``members`` may overlap between areas. A dimension referenced from two facts
    — ``products`` from both sales and inventory — belongs in both wide models,
    exactly as a conformed dimension belongs in two star schemas. Forcing a
    single owner would strip real columns off one of the models.
    """

    name: str
    anchor: str
    members: tuple[str, ...]
    anchor_score: float
    new_tables: int = 0
    """Tables this area was the first to cover, in selection order. The marginal
    gain that bought it a slot, kept so the report can show what it added."""

    estimated_fields: int = 0
    """What it was priced at when selected. Paired with ``new_tables`` this is
    the whole case for the model existing: eight fields for three new tables is
    a buy, forty for two is not."""

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class Clustering:
    areas: tuple[SubjectArea, ...]
    unassigned: tuple[str, ...] = ()
    stop_reason: StopReason = StopReason.NO_CANDIDATES
    total_table_count: int = 0
    selector: str = "greedy"
    """Who chose the anchors. A layer whose anchors came from a language model
    should not be indistinguishable from one that did not."""

    @property
    def covered(self) -> frozenset[str]:
        return frozenset(t for area in self.areas for t in area.members)

    @property
    def covered_table_count(self) -> int:
        return len(self.covered)

    @property
    def coverage(self) -> float:
        if not self.total_table_count:
            return 0.0
        return self.covered_table_count / self.total_table_count


def build_lattice(
    graph: SchemaGraph,
    profiles: tuple[TableProfile, ...],
    *,
    max_hops: int = 3,
    max_fields: int = MAX_MODEL_FIELDS,
    include_aggregates: bool = True,
    expose_child_filters: bool = False,
) -> CandidateLattice:
    """Price and measure every table as a potential anchor.

    ``max_fields`` is the cap the models will later be composed under, and is
    needed here rather than only in ``compose``: a candidate whose fields would
    be trimmed does not cost what its raw column count suggests.

    ``include_aggregates`` 와 ``expose_child_filters`` 는 뒤이어 ``compose`` 가
    쓸 값과 같아야 한다. 이 둘이 도달 범위와 가격을 동시에 바꾸기 때문이다 —
    집계를 끄면 자식은 흡수되지 않으므로 커버리지에서 빠져야 하고, 필터를 켜면
    자식 컬럼 수만큼 필드가 늘어나므로 가격에 들어가야 한다.
    """
    return CandidateLattice(
        candidates=tuple(
            Candidate(
                name=p.name,
                role=p.role.value,
                score=p.score,
                reach=reachable_tables(
                    graph,
                    p.name,
                    max_hops=max_hops,
                    include_children=include_aggregates,
                ),
                estimated_fields=max(
                    estimate_fields(
                        graph,
                        p.name,
                        max_hops=max_hops,
                        cap=max_fields,
                        include_aggregates=include_aggregates,
                        expose_child_filters=expose_child_filters,
                    ),
                    1,
                ),
            )
            for p in profiles
        ),
        total_table_count=len(graph.schema.tables),
    )


def cluster(
    graph: SchemaGraph,
    profiles: tuple[TableProfile, ...],
    *,
    policy: SelectionPolicy | None = None,
    selector: Selector | None = None,
    max_hops: int = 3,
    max_fields: int = MAX_MODEL_FIELDS,
    include_aggregates: bool = True,
    expose_child_filters: bool = False,
) -> Clustering:
    """앵커 테이블을 선택하고 이들이 커버하는 물리 영역 및 통계를 산출합니다."""
    rules = policy or SelectionPolicy()
    total = len(graph.schema.tables)

    if not profiles or total == 0:
        return Clustering(
            areas=(),
            unassigned=tuple(sorted(p.name for p in profiles)),
            stop_reason=StopReason.NO_CANDIDATES,
            total_table_count=total,
        )

    lattice = build_lattice(
        graph,
        profiles,
        max_hops=max_hops,
        max_fields=max_fields,
        include_aggregates=include_aggregates,
        expose_child_filters=expose_child_filters,
    )
    selection = (selector or GreedySelector()).select(lattice, rules)

    # Re-measure rather than trust. Marginal gain is recomputed in the order the
    # selector returned, so `new_tables` describes this layer even when the
    # anchors came from somewhere with no notion of what was covered first.
    areas: list[SubjectArea] = []
    covered: set[str] = set()
    taken_names: set[str] = set()
    for choice in selection.choices:
        candidate = lattice.get(choice.anchor)
        if candidate is None:
            continue
        areas.append(
            SubjectArea(
                name=_unique_name(
                    choice.name or candidate.name, candidate.name, taken_names
                ),
                anchor=candidate.name,
                members=tuple(sorted(candidate.reach)),
                anchor_score=candidate.score,
                new_tables=len(candidate.reach - covered),
                estimated_fields=candidate.estimated_fields,
            )
        )
        covered |= candidate.reach

    unassigned = tuple(
        sorted(p.name for p in profiles if p.name.lower() not in covered)
    )
    return Clustering(
        areas=tuple(areas),
        unassigned=unassigned,
        stop_reason=selection.stop_reason,
        total_table_count=total,
        selector=selection.label,
    )


def _unique_name(proposed: str, anchor: str, taken: set[str]) -> str:
    """모델 이름을 유일하게 만든다.

    이름은 셀렉터가 정할 수 있고(``invoice_lines`` 를 ``billing`` 이라 부르는 식),
    셀렉터는 두 앵커에 같은 이름을 줄 수 있다. 그대로 두면 ``LogicalLayer.model``
    이 첫 번째 것만 돌려주고 두 번째 모델은 이름으로 닿을 수 없게 된다 — 확장은
    그 모델을 영원히 찾지 못하고, 레이어는 있다고 보고한다.

    먼저 앵커 이름으로 물러서고, 그것마저 겹치면 번호를 붙인다.
    """
    for name in (proposed, anchor):
        if name.lower() not in taken:
            taken.add(name.lower())
            return name
    suffix = 2
    while f"{anchor}_{suffix}".lower() in taken:
        suffix += 1
    name = f"{anchor}_{suffix}"
    taken.add(name.lower())
    return name


def reachable_tables(
    graph: SchemaGraph, anchor: str, *, max_hops: int, include_children: bool = True
) -> frozenset[str]:
    """Tables a model anchored on *anchor* can absorb.

    Two directions, and they are not symmetric:

    * **Forwards** (many-to-one, up to ``max_hops``) — the referenced row is
      unique, so its columns can be inlined as fields without changing the
      anchor's grain.
    * **Backwards, one step** (one-to-many) — a child fans out, so it can only
      contribute aggregates. It is still absorbed, just not inlined.

    Children of children are excluded. There is no grain-preserving way to
    surface a grandchild's columns at the anchor, so claiming coverage of one
    would overstate what the model actually exposes.

    ``include_children`` 는 ``compose`` 의 ``include_aggregates`` 와 짝이다.
    집계를 끄면 자식은 어떤 필드도 내지 않으므로 흡수됐다고 말할 수 없다. 이
    둘이 어긋난 동안 retail 은 커버리지 66%(35/53)를 보고했지만 모델이 실제로
    안은 테이블은 13개였다.
    """
    absorbed = {anchor.lower()}
    absorbed |= {
        table.lower() for table, _ in graph.walk_many_to_one(anchor, max_hops=max_hops)
    }
    if include_children:
        absorbed |= {table.lower() for table, _ in graph.children(anchor)}
    return frozenset(absorbed)
