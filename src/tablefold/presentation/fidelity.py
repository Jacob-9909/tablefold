"""접힌 레이어가 원래 스키마를 얼마나 대변하는지 잰다.

압축 엔진에는 두 개의 축이 있어야 한다. 얼마나 줄었는가(rate)와 무엇을 잃었는가
(distortion)다. tablefold 는 오랫동안 앞의 축만 갖고 있었고, 그래서 "테이블을 버려서
비율을 좋게 만드는" 개선이 개선처럼 보였다. 이 모듈이 뒤의 축이다.

네 가지를 잰다. 서로 다른 것을 재므로 하나로 합치지 않는다:

* **컬럼 보존율** — 물리 컬럼 중 논리 필드로 꺼낼 수 있는 비율. 버려진 컬럼은
  조인 키(구조적으로는 살아 있음)와 값 컬럼(정말 사라짐)으로 나눠 센다. 이 둘을
  섞으면 대리키가 많은 스키마가 부당하게 나쁘게 나온다.

* **조인 흡수율** — 물리 FK 엣지 중 양 끝이 한 모델 안에 함께 들어간 비율.
  흡수되지 않은 엣지는 읽는 쪽이 여전히 직접 조인해야 하는 자리다.

* **질문 가능 쌍 대비 답변 가능 쌍** — 이게 핵심이다. 스키마를 얼마나 담았느냐가
  아니라 *무엇을 물을 수 있느냐*를 잰다. 그래프에서 서로 가까운(=한 질문에 함께
  등장할 법한) 테이블 쌍 중, 조인 없이 한 모델만 읽어서 답할 수 있는 쌍의 비율이다.

  팩트를 앵커로 삼은 레이어가 골드셋에서 팩트 간 질문 12건을 거부한 이유가 정확히
  이 수치에 잡힌다. ``F_SALES`` 와 ``F_MGMT_PLAN`` 은 ``D_SA_ORG`` 를 사이에 두고
  거리 2라 분모에 들어가지만, 어느 모델도 둘을 함께 담지 않아 분자에서 빠진다.

* **팬아웃 방어 수** — 집계로 접힌 1:N 자식의 수. 인라인했다면 앵커의 행이 불어나
  모든 합계가 조용히 틀어졌을 자리들이다.

의도적으로 하나의 점수로 합치지 않는다. 합치는 순간 가중치가 결론을 정하고, 그
가중치를 정당화할 근거가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass

from tablefold.graph.graph import SchemaGraph
from tablefold.presentation.cost import is_noise
from tablefold.schema.ir import Cardinality, LogicalLayer

DEFAULT_QUESTION_HOPS = 2
"""한 질문에 함께 등장할 법한 테이블 사이의 최대 거리.

1 이면 직접 FK 로 이어진 쌍만 세는데, 그러면 조인 흡수율과 같은 값이 된다.
2 면 공통 차원을 사이에 둔 팩트 쌍이 들어온다 — 웨어하우스 질의의 대부분이
여기다. 3 이상은 사람이 한 질문으로 묶지 않는 쌍까지 세기 시작한다.
"""


@dataclass(frozen=True)
class TableFidelity:
    """물리 테이블 하나가 레이어에 얼마나 남았는지."""

    table: str
    total_columns: int
    exposed: tuple[str, ...]
    """논리 필드로 꺼내거나 조건에 걸 수 있는 컬럼."""

    dropped_keys: tuple[str, ...]
    """필드로는 안 보이지만 조인/기본 키로 쓰이는 컬럼. 구조는 살아 있다."""

    dropped_values: tuple[str, ...]
    """어디에도 안 남은 컬럼. 이 테이블에 대해 잃어버린 것."""

    dropped_noise: tuple[str, ...]
    """어떤 질문에도 답하지 않아 일부러 뺀 컬럼 (``LOAD_DT`` 같은 적재 메타).

    잃은 것으로 세지 않는다. 세면 잡음을 빼는 개선이 지표를 나쁘게 만든다."""

    in_models: tuple[str, ...]
    """이 테이블을 담은 모델들. 비어 있으면 Tier-2 로 남았다는 뜻."""

    @property
    def answerable_columns(self) -> int:
        """답을 담을 수 있는 컬럼. 잡음은 분모에서 뺀다."""
        return self.total_columns - len(self.dropped_noise)

    @property
    def retention(self) -> float:
        if not self.answerable_columns:
            return 0.0
        return len(self.exposed) / self.answerable_columns


@dataclass(frozen=True)
class Fidelity:
    tables: tuple[TableFidelity, ...]

    total_columns: int
    exposed_columns: int
    dropped_key_columns: int
    dropped_value_columns: int
    dropped_noise_columns: int

    total_edges: int
    absorbed_edges: int

    askable_pairs: int
    answerable_pairs: int
    unanswerable: tuple[tuple[str, str], ...]
    """조인 없이 답할 수 없는 테이블 쌍. 레이어의 사각지대를 이름으로 보여준다.

    화면에 다 늘어놓을 수 없어 잘린 목록이다. 전체 개수는 ``unanswerable_total``
    을 봐야 한다 — 잘린 길이를 개수로 표시하면 사각지대가 실제보다 작아 보인다.
    """

    unanswerable_total: int

    fanout_guarded: int
    question_hops: int

    @property
    def column_retention(self) -> float:
        """답을 담을 수 있는 컬럼 중 꺼낼 수 있는 비율.

        분모에서 잡음을 뺀다. 빼지 않으면 ``LOAD_DT`` 20개를 제거하는 개선이
        보존율을 93.9%에서 71.5%로 떨어뜨려, 정확히 잘못된 것을 보상한다.
        """
        answerable = self.total_columns - self.dropped_noise_columns
        if not answerable:
            return 0.0
        return self.exposed_columns / answerable

    @property
    def join_absorption(self) -> float:
        if not self.total_edges:
            return 1.0
        return self.absorbed_edges / self.total_edges

    @property
    def pair_answerability(self) -> float:
        if not self.askable_pairs:
            return 1.0
        return self.answerable_pairs / self.askable_pairs


def measure(
    layer: LogicalLayer,
    graph: SchemaGraph,
    *,
    question_hops: int = DEFAULT_QUESTION_HOPS,
    max_unanswerable: int = 40,
) -> Fidelity:
    """레이어가 물리 스키마를 얼마나 대변하는지 잰다."""
    members = _members_by_model(layer)
    exposed = _exposed_columns(layer)
    key_columns = _key_columns(graph)

    tables: list[TableFidelity] = []
    total = exposed_n = dropped_key_n = dropped_value_n = noise_n = 0

    for table in graph.schema.tables:
        low = table.name.lower()
        shown = exposed.get(low, frozenset())
        keys = key_columns.get(low, frozenset())

        kept = tuple(c.name for c in table.columns if c.name.lower() in shown)
        lost = tuple(c.name for c in table.columns if c.name.lower() not in shown)
        lost_noise = tuple(c for c in lost if is_noise(c))
        lost_keys = tuple(
            c for c in lost if c.lower() in keys and not is_noise(c)
        )
        lost_values = tuple(
            c for c in lost if c.lower() not in keys and not is_noise(c)
        )

        tables.append(
            TableFidelity(
                table=table.name,
                total_columns=len(table.columns),
                exposed=kept,
                dropped_keys=lost_keys,
                dropped_values=lost_values,
                dropped_noise=lost_noise,
                in_models=tuple(
                    name for name, group in members.items() if low in group
                ),
            )
        )
        total += len(table.columns)
        exposed_n += len(kept)
        dropped_key_n += len(lost_keys)
        dropped_value_n += len(lost_values)
        noise_n += len(lost_noise)

    absorbed, edge_total = _edge_absorption(graph, members)
    askable = _askable_pairs(graph, hops=question_hops)
    answerable = {p for p in askable if any(p <= group for group in members.values())}
    missed = sorted(askable - answerable)

    return Fidelity(
        tables=tuple(tables),
        total_columns=total,
        exposed_columns=exposed_n,
        dropped_key_columns=dropped_key_n,
        dropped_value_columns=dropped_value_n,
        dropped_noise_columns=noise_n,
        total_edges=edge_total,
        absorbed_edges=absorbed,
        askable_pairs=len(askable),
        answerable_pairs=len(answerable),
        unanswerable=tuple(tuple(sorted(p)) for p in missed[:max_unanswerable]),  # type: ignore[misc]
        unanswerable_total=len(missed),
        fanout_guarded=_fanout_guarded(layer),
        question_hops=question_hops,
    )


# ── 내부 ──────────────────────────────────────────────────────────────────────


def _members_by_model(layer: LogicalLayer) -> dict[str, frozenset[str]]:
    return {
        m.name: frozenset(
            {m.base_table.lower()} | {t.lower() for t in m.absorbed_tables}
        )
        for m in layer.models
    }


def _exposed_columns(layer: LogicalLayer) -> dict[str, frozenset[str]]:
    """물리 ``table -> {column}``. 필터 전용 필드도 꺼낼 수 있는 것으로 센다.

    값으로 SELECT 하지는 못해도 조건은 걸 수 있으므로, 질문에 답하는 능력의
    관점에서는 살아 있는 컬럼이다.
    """
    found: dict[str, set[str]] = {}
    for model in layer.models:
        for f in model.fields:
            found.setdefault(f.source.table.lower(), set()).add(f.source.column.lower())
    return {k: frozenset(v) for k, v in found.items()}


def _key_columns(graph: SchemaGraph) -> dict[str, frozenset[str]]:
    """조인이나 기본 키로 쓰이는 컬럼. 필드로 안 나와도 구조는 남아 있다."""
    keys: dict[str, set[str]] = {}
    for table in graph.schema.tables:
        keys.setdefault(table.name.lower(), set()).update(
            c.lower() for c in table.primary_key
        )
    for fk in graph.schema.foreign_keys:
        keys.setdefault(fk.from_table.lower(), set()).update(
            c.lower() for c in fk.from_columns
        )
        keys.setdefault(fk.to_table.lower(), set()).update(
            c.lower() for c in fk.to_columns
        )
    return {k: frozenset(v) for k, v in keys.items()}


def _edge_absorption(
    graph: SchemaGraph, members: dict[str, frozenset[str]]
) -> tuple[int, int]:
    """양 끝이 한 모델 안에 함께 든 FK 엣지의 수와 전체 수.

    엣지는 FK 단위로 센다. 테이블 쌍으로 뭉치면 ``orders.buyer_id`` 와
    ``orders.seller_id`` 가 둘 다 ``users`` 를 가리킬 때 두 개가 하나로 세어져,
    "22개 관계 중 …" 이라는 표시가 실제 FK 수와 어긋난다. 둘은 서로 다른 조인이고
    모델이 둘 다 품어야 흡수된 것이다.

    자기 참조는 뺀다 — 같은 테이블 안의 관계는 읽는 쪽에 조인 부담을 만들지 않는다.
    """
    edges: set[tuple[str, tuple[str, ...], str]] = set()
    for fk in graph.schema.foreign_keys:
        if fk.from_table.lower() == fk.to_table.lower():
            continue
        edges.add(
            (
                fk.from_table.lower(),
                tuple(c.lower() for c in fk.from_columns),
                fk.to_table.lower(),
            )
        )
    absorbed = sum(
        1
        for source, _, target in edges
        if any({source, target} <= group for group in members.values())
    )
    return absorbed, len(edges)


def _askable_pairs(graph: SchemaGraph, *, hops: int) -> set[frozenset[str]]:
    """FK 그래프에서 거리 ``hops`` 이내인 테이블 쌍. 방향은 무시한다.

    질문은 FK 방향을 따르지 않는다 — "이 조직의 매출과 계획"은 두 팩트에서
    차원으로 각각 올라가는 모양이고, 어느 쪽도 상대를 참조하지 않는다.
    """
    adjacency: dict[str, set[str]] = {
        t.name.lower(): set() for t in graph.schema.tables
    }
    for fk in graph.schema.foreign_keys:
        a, b = fk.from_table.lower(), fk.to_table.lower()
        if a == b or a not in adjacency or b not in adjacency:
            continue
        adjacency[a].add(b)
        adjacency[b].add(a)

    pairs: set[frozenset[str]] = set()
    for start in adjacency:
        frontier = {start}
        seen = {start}
        for _ in range(hops):
            frontier = {n for node in frontier for n in adjacency[node]} - seen
            if not frontier:
                break
            seen |= frontier
            pairs.update(frozenset({start, node}) for node in frontier)
    return pairs


def _fanout_guarded(layer: LogicalLayer) -> int:
    """집계로 접힌 1:N 자식 테이블의 수 (모델별로 셈)."""
    guarded: set[tuple[str, str]] = set()
    for model in layer.models:
        for f in model.fields:
            if any(s.cardinality is Cardinality.ONE_TO_MANY for s in f.source.path):
                guarded.add((model.name, f.source.table.lower()))
    return len(guarded)


def to_dict(fidelity: Fidelity) -> dict:
    """UI 와 리포트가 함께 쓰는 직렬화 형태."""
    return {
        "column_retention": round(fidelity.column_retention, 4),
        "join_absorption": round(fidelity.join_absorption, 4),
        "pair_answerability": round(fidelity.pair_answerability, 4),
        "counts": {
            "total_columns": fidelity.total_columns,
            "exposed_columns": fidelity.exposed_columns,
            "dropped_key_columns": fidelity.dropped_key_columns,
            "dropped_value_columns": fidelity.dropped_value_columns,
            "dropped_noise_columns": fidelity.dropped_noise_columns,
            "total_edges": fidelity.total_edges,
            "absorbed_edges": fidelity.absorbed_edges,
            "askable_pairs": fidelity.askable_pairs,
            "answerable_pairs": fidelity.answerable_pairs,
            "fanout_guarded": fidelity.fanout_guarded,
            "question_hops": fidelity.question_hops,
            "unanswerable_total": fidelity.unanswerable_total,
        },
        "unanswerable": [list(p) for p in fidelity.unanswerable],
        "unanswerable_total": fidelity.unanswerable_total,
        "tables": [
            {
                "table": t.table,
                "total_columns": t.total_columns,
                "retention": round(t.retention, 4),
                "exposed": list(t.exposed),
                "dropped_keys": list(t.dropped_keys),
                "dropped_values": list(t.dropped_values),
                "dropped_noise": list(t.dropped_noise),
                "in_models": list(t.in_models),
            }
            for t in fidelity.tables
        ],
    }


def render_report(fidelity: Fidelity) -> str:
    """사람이 읽는 요약. 세 수치를 나란히 둔다 — 하나만으로는 뜻이 없다."""
    c = fidelity
    lines = [
        f"컬럼 보존   {c.column_retention * 100:5.1f}%  "
        f"({c.exposed_columns}/{c.total_columns - c.dropped_noise_columns} 노출, "
        f"키 {c.dropped_key_columns} 구조보존, 값 {c.dropped_value_columns} 소실, "
        f"잡음 {c.dropped_noise_columns} 제외)",
        f"조인 흡수   {c.join_absorption * 100:5.1f}%  "
        f"({c.absorbed_edges}/{c.total_edges} 엣지가 모델 안으로 들어감)",
        f"답변 가능   {c.pair_answerability * 100:5.1f}%  "
        f"({c.answerable_pairs}/{c.askable_pairs} 쌍, 거리 {c.question_hops} 이내)",
        f"팬아웃 방어 {c.fanout_guarded:5d}   개의 1:N 자식이 집계로 접힘",
    ]
    if c.unanswerable:
        lines.append("")
        lines.append(f"조인 없이 답할 수 없는 쌍 {c.unanswerable_total}개:")
        lines.extend(f"  {a} × {b}" for a, b in c.unanswerable[:10])
        if c.unanswerable_total > 10:
            lines.append(f"  … 외 {c.unanswerable_total - 10}쌍")
    return "\n".join(lines)
