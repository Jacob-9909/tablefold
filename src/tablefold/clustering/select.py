"""Who anchors the models, and who decides.

Anchor selection splits in two. Building the **candidate lattice** — every
table, what it reaches, what it would cost — is a graph walk with one answer.
Choosing from it is a judgement, and there is more than one defensible way to
make it, so it sits behind :class:`Selector`.

:class:`GreedySelector` is the default and needs nothing. :class:`LLMSelector`
exists because the lattice cannot express everything that matters: that
``payments``, ``invoices`` and ``returns`` are one billing story rather than
three, that ``invoice_lines`` is a poor name for a model a person will read,
that nobody in this company asks about campaigns. Those are semantic calls, and
a foreign-key graph has no opinion on them.

What the LLM is *not* trusted with is anything countable. It picks names from a
closed set; unknown names are dropped, coverage is recomputed from the graph
rather than read from the reply, and the grain rules in :mod:`tablefold.compose`
and :mod:`tablefold.expand` never see its output. The worst a bad completion can
do is choose a poor set of real anchors — never a wrong number.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

# How much a candidate's fact score can bend the coverage ranking. A pure
# coverage sort would happily anchor on a table with no measures just because it
# sits on a hub; this keeps measure-bearing tables ahead when coverage is close.
_SCORE_INFLUENCE = 0.5


class StopReason(StrEnum):
    """Why selection stopped. Reported so a surprising layer explains itself."""

    COVERAGE_REACHED = "coverage_reached"
    """The coverage target was met. The normal outcome."""

    GAIN_EXHAUSTED = "gain_exhausted"
    """Nothing left clears the admission rules. Raising the target cannot help;
    only relaxing ``min_gain`` / ``max_fields_per_table`` / ``max_hops`` can."""

    MAX_AREAS = "max_areas"
    """The hard ceiling bound before the target was met."""

    NO_CANDIDATES = "no_candidates"
    """The schema has no tables."""

    SELECTOR_CHOSE = "selector_chose"
    """A selector returned an explicit set. The count is its judgement, not the
    result of a stopping rule."""


@dataclass(frozen=True)
class SelectionPolicy:
    """When to stop adding anchors.

    ``coverage_target`` is the goal. The other two are admission rules a
    candidate has to clear before it is ranked at all — one on what it brings,
    one on what it charges:

    * ``min_gain`` — new tables an extra model must cover. The knob that finds
      the knee of the coverage curve.
    * ``max_fields_per_table`` — fields it may spend per new table. Without it,
      the fixture bought three models at ~80% overlap with ``orders``, each
      spending forty-odd fields to reach two tables nothing else covered.

    Ranking deliberately stays on gain rather than on gain-per-field. Dividing
    by price optimises for cheap coverage, which was measured to produce ten
    models anchored on ``regions``, ``carriers`` and ``payment_methods`` — a
    third of the tokens, and nothing a person would ask a question about.

    ``max_areas`` is a safety valve, not the plan. Left at ``None`` the model
    count is an output of the objective rather than an input to it.
    """

    coverage_target: float = 0.90
    min_gain: int = 2
    max_fields_per_table: float = 10.0
    max_areas: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.coverage_target <= 1.0:
            raise ValueError(
                f"coverage_target must be in [0, 1], got {self.coverage_target}"
            )
        if self.min_gain < 1:
            raise ValueError(f"min_gain must be at least 1, got {self.min_gain}")
        if self.max_fields_per_table <= 0:
            raise ValueError(
                "max_fields_per_table must be positive, got "
                f"{self.max_fields_per_table}"
            )
        if self.max_areas is not None and self.max_areas < 1:
            raise ValueError(f"max_areas must be at least 1, got {self.max_areas}")


# ── the lattice ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """잠재적 앵커 후보 테이블 및 그 비용/도달범위 데이터."""

    name: str
    role: str
    score: float
    reach: frozenset[str]
    estimated_fields: int

    @property
    def fields_per_table(self) -> float:
        return self.estimated_fields / len(self.reach) if self.reach else 0.0


@dataclass(frozen=True)
class CandidateLattice:
    """선택 가능한 모든 앵커 및 해당 앵커 선택 시 얻게 되는
    이점/비용 정보의 후보 격자.
    """

    candidates: tuple[Candidate, ...]
    total_table_count: int

    def get(self, name: str) -> Candidate | None:
        lowered = name.lower()
        return next((c for c in self.candidates if c.name.lower() == lowered), None)

    def gain(self, name: str, covered: frozenset[str]) -> int:
        found = self.get(name)
        return len(found.reach - covered) if found else 0

    def render(self, *, limit: int = 40, members: int = 12) -> str:
        """프롬프트에 포함하기 적절한 표 형식 텍스트로 격자를 렌더링합니다."""
        ranked = sorted(self.candidates, key=lambda c: (-len(c.reach), c.name))[:limit]

        lines = [
            f"{self.total_table_count} physical tables. "
            f"{len(self.candidates)} possible anchors, {len(ranked)} shown.",
            "",
            f"{'anchor':<24}{'role':<11}{'reaches':>8}{'fields':>7}"
            f"{'f/table':>9}  absorbs",
        ]
        for candidate in ranked:
            listed = sorted(candidate.reach - {candidate.name.lower()})
            shown = ", ".join(listed[:members])
            if len(listed) > members:
                shown += f", +{len(listed) - members} more"
            lines.append(
                f"{candidate.name:<24}{candidate.role:<11}{len(candidate.reach):>8}"
                f"{candidate.estimated_fields:>7}{candidate.fields_per_table:>9.1f}"
                f"  {shown}"
            )
        return "\n".join(lines)


# ── the contract ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Choice:
    """An anchor, and what to call the model built on it."""

    anchor: str
    name: str | None = None
    """A semantic name for the model. ``None`` keeps the anchor's table name,
    which is what the greedy selector always does — it has no basis for a
    better one."""


@dataclass(frozen=True)
class Selection:
    choices: tuple[Choice, ...]
    stop_reason: StopReason
    label: str
    """Who chose, for the report. A layer whose anchors came from a language
    model should not be indistinguishable from one that did not."""


class SelectionError(Exception):
    """A selector could not produce a usable set of anchors."""


class Selector(Protocol):
    def select(self, lattice: CandidateLattice, policy: SelectionPolicy) -> Selection:
        """Choose anchors from *lattice*, best first."""
        ...


# ── explicit ──────────────────────────────────────────────────────────────────


class ExplicitSelector:
    """호출자가 앵커를 직접 지정한다.

    스타 스키마에서는 앵커가 자명하다 — 팩트 테이블이 곧 앵커고, 차원은 그 위로
    인라인된다. 그런 스키마에서 탐욕적 집합 커버를 돌리는 것은 이미 아는 답을
    다시 찾는 일이고, 커버리지 목표나 ``min_gain`` 같은 정지 규칙이 오히려
    의도한 앵커를 탈락시킬 수 있다.

    래티스에 없는 이름은 조용히 버린다. :class:`LLMSelector` 와 같은 규칙이며,
    이유도 같다 — 존재하지 않는 앵커는 ``compose`` 까지 흘러가서 거기서 터지느니
    선택 단계에서 사라지는 편이 낫다.
    """

    def __init__(self, anchors: tuple[str, ...] | list[str]) -> None:
        self._anchors = tuple(anchors)

    def select(self, lattice: CandidateLattice, policy: SelectionPolicy) -> Selection:
        if not lattice.candidates:
            return Selection((), StopReason.NO_CANDIDATES, self.label)

        chosen: list[Choice] = []
        seen: set[str] = set()
        for name in self._anchors:
            candidate = lattice.get(name)
            if candidate is None or candidate.name.lower() in seen:
                continue
            seen.add(candidate.name.lower())
            chosen.append(Choice(anchor=candidate.name))
            if policy.max_areas is not None and len(chosen) >= policy.max_areas:
                break

        return Selection(tuple(chosen), StopReason.SELECTOR_CHOSE, self.label)

    @property
    def label(self) -> str:
        return "explicit"


# ── greedy ────────────────────────────────────────────────────────────────────


class GreedySelector:
    """탐욕적 최대 커버리지(Greedy Maximum Coverage) 알고리즘으로 앵커를 선택합니다."""

    def select(self, lattice: CandidateLattice, policy: SelectionPolicy) -> Selection:
        if not lattice.candidates:
            return Selection((), StopReason.NO_CANDIDATES, self.label)

        import math

        needed = math.ceil(policy.coverage_target * lattice.total_table_count)

        chosen: list[Choice] = []
        taken: set[str] = set()
        covered: set[str] = set()
        reason = StopReason.GAIN_EXHAUSTED

        while True:
            if len(covered) >= needed:
                reason = StopReason.COVERAGE_REACHED
                break
            if policy.max_areas is not None and len(chosen) >= policy.max_areas:
                reason = StopReason.MAX_AREAS
                break

            best: Candidate | None = None
            best_value = 0.0

            for candidate in lattice.candidates:
                if candidate.name in taken:
                    continue
                gain = len(candidate.reach - covered)
                if gain < policy.min_gain:
                    continue
                if candidate.estimated_fields / gain > policy.max_fields_per_table:
                    continue
                # Ranking stays on gain, because the goal is the fewest models
                # and gain is what makes a model unnecessary. Price is an
                # admission rule above, not a divisor here.
                value = gain * (1.0 + _SCORE_INFLUENCE * candidate.score)
                if value > best_value:
                    best, best_value = candidate, value

            if best is None:
                reason = StopReason.GAIN_EXHAUSTED
                break

            chosen.append(Choice(anchor=best.name))
            taken.add(best.name)
            covered |= best.reach

        return Selection(tuple(chosen), reason, self.label)

    @property
    def label(self) -> str:
        return "greedy"


# ── LLM ───────────────────────────────────────────────────────────────────────

_PROMPT = """\
You are grouping a database schema into a small number of wide "models" for a \
text-to-SQL system. Each model sits at one anchor table's grain and absorbs the \
tables it reaches, so a reader can answer questions without reasoning about joins.

{lattice}

Rules for your choice:
- Pick the FEWEST anchors that cover the schema well. Aim for about \
{coverage:.0%} of the {total} tables.
- Every anchor must bring at least {min_gain} tables no earlier anchor covers. \
An anchor overlapping an earlier one almost entirely is waste.
- Spend at most about {max_cost:.0f} fields per table an anchor is the first to \
cover. The "fields" and "f/table" columns are what it costs a reader.
- Prefer anchors a person would ask questions about (orders, customers, \
products) over lookup tables (regions, carriers, tax rates).
- Where several candidates are one business story, pick the one whose reach \
covers the others rather than listing all of them.
- Give each model a short, lowercase, plural name for what it is about. It does \
not have to be the anchor's table name.

Reply with JSON only, no prose:
{{"anchors": [{{"table": "orders", "name": "sales"}}, ...]}}
"""


class LLMSelector:
    """Anchor selection by a language model, held to the lattice.

    ``complete`` takes a prompt and returns the model's text. Keeping it a plain
    callable means this module depends on no vendor SDK and can be tested
    against a fixed reply; see :mod:`tablefold.llm` for a ready adapter.

    ``fallback`` runs when the reply names nothing real. A schema that cannot be
    folded because a completion came back malformed would be a bad trade, so the
    default is to fall back to greedy rather than to fail.
    """

    def __init__(
        self,
        complete: Callable[[str], str],
        *,
        fallback: Selector | None = None,
        candidate_limit: int = 40,
    ) -> None:
        self._complete = complete
        self._fallback = GreedySelector() if fallback is None else fallback
        self._limit = candidate_limit

    def select(self, lattice: CandidateLattice, policy: SelectionPolicy) -> Selection:
        if not lattice.candidates:
            return Selection((), StopReason.NO_CANDIDATES, "llm")

        prompt = _PROMPT.format(
            lattice=lattice.render(limit=self._limit),
            coverage=policy.coverage_target,
            total=lattice.total_table_count,
            min_gain=policy.min_gain,
            max_cost=policy.max_fields_per_table,
        )

        try:
            proposed = _parse_choices(self._complete(prompt))
        except SelectionError:
            proposed = ()

        # Names the lattice does not know are dropped rather than trusted. A
        # hallucinated table would otherwise reach compose and fail there, far
        # from the thing that invented it.
        resolved: list[Choice] = []
        seen: set[str] = set()
        for choice in proposed:
            candidate = lattice.get(choice.anchor)
            if candidate is None or candidate.name.lower() in seen:
                continue
            seen.add(candidate.name.lower())
            resolved.append(replace(choice, anchor=candidate.name))
            if policy.max_areas is not None and len(resolved) >= policy.max_areas:
                break

        if not resolved:
            fallen = self._fallback.select(lattice, policy)
            return replace(fallen, label=f"{fallen.label} (llm fallback)")

        return Selection(tuple(resolved), StopReason.SELECTOR_CHOSE, "llm")

    @property
    def label(self) -> str:
        return "llm"


def _parse_choices(reply: str) -> tuple[Choice, ...]:
    """Read ``{"anchors": [...]}`` out of a completion.

    Tolerant of a fenced block or surrounding prose, because instructing a model
    to emit bare JSON is a request rather than a guarantee.
    """
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if match is None:
        raise SelectionError("no JSON object in the completion")

    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise SelectionError(f"completion is not valid JSON: {exc}") from exc

    entries = payload.get("anchors") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise SelectionError("completion has no 'anchors' list")

    found: list[Choice] = []
    for entry in entries:
        if isinstance(entry, str):
            found.append(Choice(anchor=entry))
        elif isinstance(entry, dict) and isinstance(entry.get("table"), str):
            name = entry.get("name")
            found.append(
                Choice(
                    anchor=entry["table"],
                    name=name.strip()
                    if isinstance(name, str) and name.strip()
                    else None,
                )
            )
    return tuple(found)
