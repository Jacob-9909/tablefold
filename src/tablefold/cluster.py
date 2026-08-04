"""Choose the handful of anchors the wide models will sit on.

An anchor is a table whose grain a wide model sits at. Picking them by score
alone fails: in a connected schema the top-scoring facts are all adjacent, so
the top four are four views of the same corner and half the schema goes
uncovered.

The objective is coverage. Each fact table can absorb a known set of
neighbours — everything reachable by following its foreign keys forwards, plus
its immediate children — so anchor selection is a **maximum coverage** problem,
solved greedily: repeatedly take the fact that absorbs the most tables nothing
has absorbed yet, weighted by how fact-like it is.

Greedy max-coverage is the standard (1 - 1/e) approximation. That bound is
ample here, where the count of anchors is 3-5 and the alternative is a human
guessing at subject areas.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.classify import TableProfile
from tablefold.graph import SchemaGraph

# How much a candidate's fact score can bend the coverage ranking. A pure
# coverage sort would happily anchor on a table with no measures just because it
# sits on a hub; this keeps measure-bearing tables ahead when coverage is close.
_SCORE_INFLUENCE = 0.5

# Referencing tables at which a dimension is also eligible to anchor a model.
# ``customers`` is not a fact — it has no measures and describes a thing rather
# than an event — but eight tables point at it, and a customer-grained model
# absorbs all eight. Restricting anchors to facts leaves that entire half of the
# schema unreachable, so hubs join the candidate pool and compete on coverage.
HUB_IN_DEGREE = 3


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

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class Clustering:
    areas: tuple[SubjectArea, ...]
    unassigned: tuple[str, ...] = ()

    @property
    def covered(self) -> frozenset[str]:
        return frozenset(t for area in self.areas for t in area.members)

    @property
    def covered_table_count(self) -> int:
        return len(self.covered)


def cluster(
    graph: SchemaGraph,
    profiles: tuple[TableProfile, ...],
    *,
    target_areas: int = 4,
    max_hops: int = 3,
) -> Clustering:
    """Pick up to *target_areas* anchors that jointly cover the most tables.

    ``target_areas`` is a ceiling, not a quota. Selection stops early once no
    remaining fact table absorbs anything new — adding an anchor that covers
    nothing would cost the reader a model and tell them nothing.
    """
    if not profiles:
        return Clustering(areas=())

    candidates = [p for p in profiles if p.is_fact or p.in_degree >= HUB_IN_DEGREE]
    if not candidates:
        return Clustering(areas=(), unassigned=tuple(p.name for p in profiles))

    reach = {
        p.name: reachable_tables(graph, p.name, max_hops=max_hops) for p in candidates
    }

    chosen: list[TableProfile] = []
    covered: set[str] = set()

    while len(chosen) < target_areas:
        best: TableProfile | None = None
        best_value = 0.0

        for candidate in candidates:
            if any(candidate.name == c.name for c in chosen):
                continue
            gain = len(reach[candidate.name] - covered)
            if gain == 0:
                continue
            value = gain * (1.0 + _SCORE_INFLUENCE * candidate.score)
            if value > best_value:
                best, best_value = candidate, value

        if best is None:
            break

        chosen.append(best)
        covered |= reach[best.name]

    areas = tuple(
        SubjectArea(
            name=anchor.name,
            anchor=anchor.name,
            members=tuple(sorted(reach[anchor.name])),
            anchor_score=anchor.score,
        )
        for anchor in chosen
    )
    unassigned = tuple(sorted(p.name for p in profiles if p.name not in covered))
    return Clustering(areas=areas, unassigned=unassigned)


def reachable_tables(
    graph: SchemaGraph, anchor: str, *, max_hops: int
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
    """
    absorbed = {anchor.lower()}
    absorbed |= {
        table.lower() for table, _ in graph.walk_many_to_one(anchor, max_hops=max_hops)
    }
    absorbed |= {table.lower() for table, _ in graph.children(anchor)}
    return frozenset(absorbed)
