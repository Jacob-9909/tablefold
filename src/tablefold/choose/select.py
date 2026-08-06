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
rather than read from the reply, and the grain rules in :mod:`tablefold.build.compose`
and :mod:`tablefold.rewrite.expand` never see its output. The worst a bad completion can
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

    **이 값들은 :class:`GreedySelector` 에만 작용한다.** 스타 프리셋
    (:func:`~tablefold.t2sql.preset.fold_star_schema`)은 :class:`ExplicitSelector`
    로 앵커를 구조에서 정하고 ``prune_redundant`` 로 중복만 뺀다 — 거기서
    ``coverage_target`` · ``min_gain`` · ``max_fields_per_table`` 은 아무 일도
    하지 않는다. 화면과 CLI 가 이 셋을 노출하면 "돌려도 아무것도 안 바뀐다"는
    노브를 보여 주게 된다.

    그리고 이 노브들이 존재하는 근본 이유는 **목적함수의 부재**다. 골드셋이 있으면
    :mod:`tablefold.choose.tune` 이 프롬프트 길이 ↔ 답변 수의 교환곡선을 직접
    재므로, 대리 지표를 튜닝할 이유가 사라진다.
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

    inlined: frozenset[str] = frozenset()
    """``reach`` 중 **컬럼을 값으로 꺼낼 수 있는** 표. 앵커 자신과 다대일로 닿는
    표들이다.

    ``reach`` 와 나누는 이유는 1:N 자식이다. 자식도 흡수되지만 집계로만 들어오므로
    그 표의 속성 컬럼은 ``filter_only`` 가 된다 — WHERE 는 되고 SELECT/GROUP BY 는
    안 된다. "거래처별", "계정별" 같은 질문은 GROUP BY 를 요구하므로, 그 표가
    투영 가능한 앵커가 하나도 없으면 답할 방법이 없다.

    비워 두면 :func:`_drop_redundant` 가 투영 조건을 보지 않는다 — 이 값을 채우지
    않는 호출자의 동작은 예전과 같다.
    """

    provides_grain: bool = False
    """이 앵커가 다른 앵커에 없는 **입도**를 들고 온다. 중복 판정에서 면제된다.

    :func:`_drop_redundant` 는 흡수하는 *테이블 조합* 만 본다. 그래서 같은 팩트들을
    담는 두 앵커를 같은 것으로 본다 — 실측에서 ``V_PERIOD`` (월 입도)가 ``D_ORG``
    (조직 입도)에 흡수됐다고 판정되어 잘렸다. 둘의 조합은 같지만 답하는 질문은
    다르다. "조직별 매출 대 계획"과 "월별 매출 대 계획"은 서로를 대신하지 못한다.

    제대로 고치려면 판정을 테이블이 아니라 **투영 가능한 컬럼** 단위로 내려야
    한다. 지금은 근거가 확실한 경우 — 그 입도를 가진 물리 테이블이 아예 없어서
    합성한 가상 앵커 — 만 면제한다. 넓은 결함은 그대로 남아 있다.
    """

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

    ``prune_redundant`` 를 켜면 아무것도 새로 사지 않는 앵커를 뺀다. 팩트와 차원을
    모두 앵커로 주는 스타 스키마에서 필요하다 — 그 조합은 대개 절반이 낭비다.
    NL2SQL 19테이블에서 팩트 앵커 10개 중 5개는 답할 수 있는 질문을 **하나도**
    늘리지 않았다. 이미 차원 앵커가 같은 조합을 담고 있었기 때문이다. 빼도
    답변가능률은 100% 그대로이고 모델만 19개에서 14개로 준다.
    """

    def __init__(
        self,
        anchors: tuple[str, ...] | list[str],
        *,
        prune_redundant: bool = False,
    ) -> None:
        self._anchors = tuple(anchors)
        self._prune = prune_redundant

    def select(self, lattice: CandidateLattice, policy: SelectionPolicy) -> Selection:
        if not lattice.candidates:
            return Selection((), StopReason.NO_CANDIDATES, self.label)

        resolved: list[Candidate] = []
        seen: set[str] = set()
        for name in self._anchors:
            candidate = lattice.get(name)
            if candidate is None or candidate.name.lower() in seen:
                continue
            seen.add(candidate.name.lower())
            resolved.append(candidate)

        if self._prune:
            resolved = _drop_redundant(resolved)

        chosen = [Choice(anchor=c.name) for c in resolved]
        if policy.max_areas is not None:
            chosen = chosen[: policy.max_areas]

        return Selection(tuple(chosen), StopReason.SELECTOR_CHOSE, self.label)

    @property
    def label(self) -> str:
        return "explicit (pruned)" if self._prune else "explicit"


def _pairs_of(candidate: Candidate) -> set[frozenset[str]]:
    """이 앵커가 한 모델 안에 함께 놓는 테이블 쌍들.

    앵커가 사는 것은 테이블이 아니라 *조합* 이다. 어떤 테이블을 담고 있어도 그
    조합을 다른 앵커가 이미 담고 있으면 새로 답할 수 있게 되는 질문은 없다.
    """
    members = sorted(candidate.reach)
    return {
        frozenset((a, b))
        for i, a in enumerate(members)
        for b in members[i + 1 :]
    }


def _drop_redundant(candidates: list[Candidate]) -> list[Candidate]:
    """조합도 테이블도 투영도 새로 데려오지 않는 앵커를 뺀다.

    작게 기여하는 것부터 빼 본다. 크게 기여하는 앵커를 먼저 빼면 그 자리를
    작은 것 여럿이 메우게 되어 결과가 커진다.

    세 가지를 함께 본다. 하나라도 빠뜨리면 조용히 답을 잃는다:

    * **쌍** — 이 앵커가 한 모델에 함께 놓는 테이블 조합.
    * **테이블** — 이웃이 하나도 없는 테이블은 어떤 쌍에도 안 들어가므로, 그
      테이블을 유일하게 담은 앵커가 쌍만 보면 사라진다.
    * **투영** — 흡수됐다고 답할 수 있는 것이 아니다. 1:N 자식으로만 흡수된 표는
      컬럼이 ``filter_only`` 로 나와 GROUP BY 에 못 쓴다. NL2SQL 골드셋에서
      ``D_CUSTOMER`` · ``D_PL_ACCT`` · ``D_BS_ACCT`` 앵커가 정확히 이 이유로
      잘렸고, "거래처별" · "계정별" 질문 6건이 통째로 실패했다. 답변가능률은
      그동안 100% 를 보고했다.
    """
    if len(candidates) <= 1:
        return candidates

    pairs = {c.name: _pairs_of(c) for c in candidates}
    order = sorted(candidates, key=lambda c: (len(pairs[c.name]), c.name))

    kept = list(candidates)
    for candidate in order:
        if candidate.provides_grain:
            continue
        rest = [c for c in kept if c.name != candidate.name]
        if not rest:
            break
        covers_pairs = set().union(*(pairs[c.name] for c in rest))
        covers_tables = set().union(*(c.reach for c in rest))
        covers_inlined = set().union(*(c.inlined for c in rest))
        if (
            pairs[candidate.name] <= covers_pairs
            and candidate.reach <= covers_tables
            and candidate.inlined <= covers_inlined
        ):
            kept = rest

    # 입력 순서를 지킨다. 호출자가 준 순서에 뜻이 있을 수 있다.
    keep_names = {c.name for c in kept}
    return [c for c in candidates if c.name in keep_names]


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
    against a fixed reply; see :mod:`tablefold.report.llm` for a ready adapter.

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
