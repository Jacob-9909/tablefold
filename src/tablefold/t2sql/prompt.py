"""질문을 논리 SQL로 바꾸게 하는 프롬프트.

두 단계로 나뉜다:

1. **라우팅** — 모델 카탈로그(이름·입도·무엇을 담았나)만 보여 주고 하나를 고르게 한다
2. **작성** — 고른 모델 **하나**의 정의만 보여 주고 SQL을 쓰게 한다

레이어 전체를 매번 넣지 않는 이유는 크기다. 실측(NL2SQL 9모델)에서 레이어 전체는
26,744자인데 질의가 실제로 쓰는 것은 모델 하나뿐이다. 카탈로그 + 모델 하나면 같은
질문에 훨씬 적은 토큰으로 답한다. 대가는 LLM 호출 1회가 2회가 되는 것과, 라우팅이
틀리면 그 질문을 못 푼다는 것 — 뒤쪽은 :mod:`tablefold.t2sql.engine` 이 전체
레이어로 물러서서 메운다.

레이어 텍스트(:func:`tablefold.report.prompt.render_text`)가 스키마를 말하고,
여기서는 **계약**을 말한다. 계약은 :mod:`tablefold.rewrite.expand` 가 집행하는
규칙과 정확히 같아야 한다 — 프롬프트가 허용하는데 확장이 거부하면 재시도가
낭비되고, 프롬프트가 금지하는데 확장이 허용하면 쓸 수 있는 질의가 줄어든다.

예시는 **검증해서 넣는다**. :func:`build_prompt` 가 각 예시를 실제로 확장해 보고,
지금 레이어에서 안 되는 예시는 뺀다. 안 그러면 존재하지 않는 필드 이름을 프롬프트가
가르치게 되고, 그건 환각을 줄이는 게 아니라 만드는 것이다.

**프롬프트 캐시.** 모든 프롬프트는 :class:`Prompt` 로 나온다 — 질문이 바뀌어도
바이트가 같은 ``cached`` 접두사와, 질문마다 달라지는 ``fresh`` 꼬리. 캐시는 어느
공급자에서나 **접두사 일치**라서 안정적인 것이 앞, 휘발하는 것이 뒤여야 한다.
이 순서가 어긋나면 캐시는 에러 없이 그냥 안 붙는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from tablefold.ir import FieldKind, LogicalLayer, LogicalModel
from tablefold.relate.graph import SchemaGraph
from tablefold.report.prompt import render_model, render_text
from tablefold.rewrite.expand import ExpansionError, expand


@dataclass(frozen=True)
class Prompt:
    """캐시 경계로 갈라 둔 프롬프트.

    ``cached`` 는 질문이 바뀌어도 **바이트가 같아야 한다.** 여기에 시각이나 질문
    일부가 섞이면 접두사가 매번 달라져 캐시가 조용히 죽는다 — 에러는 없고 비용만
    는다. :mod:`tablefold.t2sql.provider` 가 이 경계에 캐시 breakpoint 를 놓는다.
    """

    cached: str
    fresh: str

    def __str__(self) -> str:
        return f"{self.cached}\n{self.fresh}"

    def __len__(self) -> int:
        return len(self.cached) + len(self.fresh) + 1


@dataclass(frozen=True)
class Example:
    """질문 하나와, 그 질문에 대한 논리 SQL 하나."""

    question: str
    logical_sql: str

    def model_name(self, layer: LogicalLayer) -> str | None:
        """이 예시가 읽는 모델 이름. 모델별 프롬프트를 채울 때 쓴다."""
        try:
            statement = sqlglot.parse_one(self.logical_sql)
        except Exception:  # noqa: BLE001 — 못 읽으면 어느 모델인지도 모른다
            return None
        for table in statement.find_all(exp.Table):
            found = layer.model(table.name)
            if found is not None:
                return found.name
        return None


# 확장이 실제로 거부하는 것만 적는다. 프롬프트의 규칙과 코드의 규칙이 갈라지면
# 둘 중 하나는 거짓말이 된다.
_CONTRACT = """\
=== 규칙 ===
1. 위 모델 **하나**만 읽는다. 물리 테이블 이름을 쓰지 않고, JOIN 을 쓰지 않는다.
   필요한 조인은 확장 엔진이 만든다. 같은 모델을 두 별칭으로 잇는 것(`FROM m a,
   m b`)도 안 된다 — 행이 곱해져 합계가 부푼다.
2. 위에 나열된 필드 이름만 쓴다. 없는 이름을 지어내면 거부된다.
3. `WHERE 전용` 필드는 `WHERE` 에서만, `AND` 로만, 조건 하나에 필드 하나로 쓴다.
   `SELECT` / `GROUP BY` / `ORDER BY` / `OR` / `CASE WHEN` 에 쓰면 거부된다.
   그래서 기간이 `WHERE 전용` 인 모델에서는 **두 기간을 한 질의에 못 담는다**
   (당월 vs 전년동월). 그런 질문은 한 기간만 답하고, 어느 기간을 답했는지
   `SELECT` 에 상수로 적는다. 기간이 `WHERE 전용` 이 아닌 일반 컬럼인 모델에서는
   `SUM(CASE WHEN 기간컬럼 LIKE '2010%' THEN 값 ELSE 0 END)` 로 나란히 놓아도 된다.
4. `..._sum` 필드는 자식 행에 대해 **이미 합산된** 값이다. 여러 행을 묶을 때
   `SUM` 을 다시 씌우는 것은 정상이고 이중 계산이 아니다. `AVG` 는 총계들의
   평균이 되어 뜻이 달라지므로 `SUM(x)/SUM(y)` 로 쓴다.
5. 나눗셈에는 0 분모를 막는다: `CASE WHEN SUM(b)=0 THEN NULL ELSE SUM(a)/SUM(b) END`.
6. `{dialect}` 문법으로 쓴다.
7. SQL만, ```sql 코드 블록 하나로 답한다. 설명을 붙이지 않는다."""

_ROUTER_RULES = """\
=== 고르는 법 ===
- 질문이 팩트 두 개를 함께 묻거나(매출 대 계획) 조직·품목 단위로 묶으면
  **차원 모델**을 고른다. 그 모델이 팩트들을 사전집계해 나란히 담고 있다.
- 전표 한 줄 단위의 상세를 묻으면 **팩트 모델**을 고른다.
- 질문이 요구하는 값이 전부 한 모델 안에 있어야 한다. 여럿에 걸치면 가장 많이
  담은 쪽을 고른다.

모델 이름 하나만 답한다. 설명도, 따옴표도, 코드 블록도 붙이지 않는다."""


# ── 라우팅 ────────────────────────────────────────────────────────────────────

# 카탈로그 한 줄에 실을 이름 수. 다 넣으면 카탈로그가 레이어만큼 커져서
# 단계를 나눈 뜻이 없어진다.
CATALOG_FIELD_SAMPLE = 12


def render_catalog(
    layer: LogicalLayer,
    *,
    compact_relations: dict[str, str] | None = None,
) -> str:
    """모델을 고르기 위한 최소 정보. 필드를 전부 늘어놓지 않는다.

    고르는 데 필요한 것은 **입도와 무엇을 답할 수 있나**다. 필드 목록 전체는 고른
    다음에 필요하다.

    측정값을 **원천 표별로 묶어 맨 앞에** 놓는다. 모델 이름만으로는 못 고른다 —
    ``D_SA_ORG`` 와 ``D_FI_ORG`` 는 둘 다 조직 차원이고, 이름은 어느 쪽이 매출을
    담는지 말해 주지 않았다. 실측에서 라우터가 "매출" 질문에 ``D_FI_ORG``(손익)를
    골랐고, ``D_WORKSHOP``(작업장)을 고른 적도 있다. ``F_SALES(SALES_AMT)`` 처럼
    원천 표를 붙이면 질문의 낱말과 바로 이어진다.

    ``compact_relations`` ({모델 이름 → 관계 설명}) 에 들어간 모델은 한 줄 요약
    으로 내려간다. OLTP 스키마의 조합 표(두 부모를 잇는 연결표)는 수십 개인데
    각자 상세 블록을 차지하면 카탈로그가 수천 자 불어난다 — Mastodon 116표
    실측에서 32% 가 이것들이었다. 내용을 지우면 관계 질문을 못 고르므로, 지우는
    대신 **관계 한 줄** 로 압축한다. 관계 문자열은 FK 그래프가 알려주는 것이고
    레이어만으로는 복원되지 않는다 — 그래서 호출자가 넘기는 값이다. ``None`` 이면
    기존 형태 그대로다.
    """
    compact = (
        {n.lower() for n in compact_relations}
        if compact_relations is not None
        else frozenset()
    )
    relations = {k.lower(): v for k, v in (compact_relations or {}).items()}
    full_models = [m for m in layer.models if m.name.lower() not in compact]
    joined_models = [m for m in layer.models if m.name.lower() in compact]

    lines = [f"=== 모델 {len(layer.models)}개 ==="]

    def render_full(model: LogicalModel) -> None:
        lines.append("")
        lines.append(f"### {model.name}")
        lines.append(f"  한 줄 = {model.base_table} 한 행")

        if is_summary_model(model):
            # 요약 모델은 행 수가 원본의 백분의 일일 수 있다. 추세 질문이 상세
            # 모델을 고르면 정답은 나오되 비용과 시간만 든다. 카탈로그에서
            # 방향을 미리 제시한다 — 판단은 라우터 몫, 근거 제시는 여기 몫.
            lines.append(
                '  ※ 월 입도 요약. "월별 추세", "기간별 흐름" 처럼 기간이'
                " 축이 되는 질문은 상세 모델보다 이쪽을 우선 고른다."
            )

        measures = _measures_by_source(model)
        if measures:
            lines.append(f"  답할 수 있는 값: {measures}")

        # 수치 컬럼은 위 "답할 수 있는 값" 에 이미 있다. 여기 또 넣으면 묶는
        # 기준으로 쓰라는 뜻이 되어 버린다.
        labels = [
            f.name
            for f in model.fields
            if f.source.kind is not FieldKind.AGGREGATED
            and not f.filter_only
            and not _is_measure(f)
        ]
        if labels:
            lines.append(f"  묶을 수 있는 기준: {_capped(labels)}")

        conditions = [f.name for f in model.fields if f.filter_only]
        if conditions:
            lines.append(f"  걸 수 있는 조건: {_capped(conditions)}")

    def render_compact(model: LogicalModel) -> None:
        relation = relations.get(model.name.lower(), model.base_table)
        lines.append(f"  {model.name} = {relation} 조합")

    for model in full_models:
        render_full(model)

    if joined_models:
        lines.append("")
        lines.append(
            f"=== 조합 표 {len(joined_models)}개 "
            "(두 대상의 관계를 세거나 나열하는 표 — 관계 질문은 여기서 고른다) ==="
        )
        for model in joined_models:
            lines.append("")
            lines.append(f"### {model.name}")
            render_compact(model)
    return "\n".join(lines)


def is_summary_model(model: LogicalModel) -> bool:
    """월 입도 요약 모델인가. 이름은 :mod:`tablefold.relate.synthesize` 가 정한다.

    설명 필드 대신 이름 규칙을 보는 이유는 단순하다 — 이 판정은 프롬프트를
    쓰는 시점에 필요한데, 요약 합성과 레이어 작성 사이에서 설명이 유실될 수
    있다. 합성기가 붙인 이름은 계약이다.
    """
    base = model.base_table
    return base.startswith("V_") and base.endswith("_MON")


def _measures_by_source(model: LogicalModel) -> str:
    """``F_SALES(SALES_AMT, SALES_QTY) · F_MGMT_PLAN(SALES_PLAN_AMT)``.

    차원 앵커는 집계 필드가 원천 팩트를 들고 있다. 팩트 앵커는 집계가 없고 자기
    수치 컬럼이 곧 측정값이므로 자기 이름으로 묶는다 — 어느 쪽이든 읽는 쪽에는
    "이 모델은 어느 표의 숫자를 답한다"로 보인다.
    """
    by_source: dict[str, list[str]] = {}
    for f in model.fields:
        if f.filter_only:
            continue
        if f.source.kind is FieldKind.AGGREGATED:
            if f.source.column == "*":
                continue  # ``..._count`` 는 어느 모델에나 있어 구별에 도움이 안 된다
            by_source.setdefault(f.source.table, []).append(f.source.column)
        elif f.source.kind is FieldKind.BASE and _is_measure(f):
            by_source.setdefault(model.base_table, []).append(f.source.column)

    return " · ".join(
        f"{table}({', '.join(dict.fromkeys(columns))})"
        for table, columns in by_source.items()
    )


def _is_measure(field) -> bool:
    """수치형 필드. 타입 문자열만 보고 판단한다 — 물리 컬럼을 다시 안 찾으려고."""
    lowered = field.type.lower()
    return any(
        token in lowered
        for token in ("int", "numeric", "decimal", "double", "float", "money", "real")
    )


def _capped(names: list[str]) -> str:
    shown = ", ".join(names[:CATALOG_FIELD_SAMPLE])
    extra = len(names) - CATALOG_FIELD_SAMPLE
    return f"{shown} 외 {extra}개" if extra > 0 else shown


def build_router_prompt(
    question: str,
    layer: LogicalLayer,
    *,
    graph=None,
) -> Prompt:
    """질문에 맞는 모델 하나를 고르게 하는 프롬프트.

    ``graph`` 를 주면 조합 표(부모 둘 이상 참조)를 찾아 카탈로그에서 한 줄로
    내린다 — OLTP 스키마에서 이것들만 수십 개라 카탈로그의 삼분의 일이
    넘는 실측이 있다.
    """
    compact_relations = None
    if graph is not None:
        compact_relations = {
            m.name: " × ".join(
                sorted({fk.to_table for fk in graph.outgoing(m.base_table)})[:3]
            )
            for m in layer.models
            if graph.out_degree(m.base_table) >= 2
        }
    return Prompt(
        cached="\n".join(
            [
                "아래는 넓은 논리 모델 목록이다. 질문에 답할 수 있는 모델을 "
                "하나 고른다.",
                "",
                render_catalog(layer, compact_relations=compact_relations),
                "",
                _ROUTER_RULES,
            ]
        ),
        fresh=f"\n=== 질문 ===\n{question}\n\n=== 모델 이름 ===",
    )


def parse_model_name(reply: str, layer: LogicalLayer) -> str | None:
    """완성 텍스트에서 모델 이름을 꺼낸다. 레이어에 없는 이름은 ``None``.

    닫힌 집합에서만 받는다 — 지어낸 이름이 다음 단계로 흘러가면 거기서 터지고,
    실패가 이름을 만든 곳에서 멀어진다.
    """
    exact = layer.model(reply.strip().strip("`\"' \n."))
    if exact is not None:
        return exact.name
    # 모델이 문장으로 답했을 때를 위해 낱말 단위로도 훑는다.
    for token in reply.replace("\n", " ").replace(",", " ").split():
        found = layer.model(token.strip("`\"'.,:;()"))
        if found is not None:
            return found.name
    return None


# ── SQL 작성 ──────────────────────────────────────────────────────────────────


def build_prompt(
    question: str,
    layer: LogicalLayer,
    graph: SchemaGraph,
    *,
    dialect: str = "postgres",
    examples: tuple[Example, ...] = (),
    model: LogicalModel | None = None,
) -> Prompt:
    """*question* 을 논리 SQL로 옮기게 하는 프롬프트.

    ``model`` 을 주면 그 모델 하나만 담고, 그 모델을 쓰는 예시만 남긴다. 비워 두면
    레이어 전체를 담는다 — 라우팅이 실패했을 때 물러설 자리다.
    """
    if model is not None:
        schema_text = render_model(model)
        examples = tuple(e for e in examples if e.model_name(layer) == model.name)
    else:
        schema_text = render_text(layer)

    blocks = [
        "너는 넓은 논리 모델을 향해 SQL을 쓰는 분석가다. 아래 모델 정의만 보고 "
        "질문에 답하는 SQL 하나를 쓴다.",
        "",
        "=== 논리 모델 ===",
        schema_text,
        _CONTRACT.format(dialect=dialect),
    ]

    usable = valid_examples(examples, layer, graph, dialect=dialect)
    if usable:
        blocks.append("")
        blocks.append("=== 예시 ===")
        for example in usable:
            blocks.append(f"질문: {example.question}")
            blocks.append(f"```sql\n{example.logical_sql.strip()}\n```")

    return Prompt(
        cached="\n".join(blocks),
        fresh=f"\n=== 질문 ===\n{question}\n\n=== SQL ===",
    )


def build_repair_prompt(base: Prompt, *, rejected_sql: str, error: str) -> Prompt:
    """거부된 SQL과 그 이유를 붙여 다시 쓰게 한다.

    확장의 거부 메시지는 사람이 읽으라고 쓴 것이 아니라 **고치라고** 쓴 것이다 —
    모르는 필드를 지목할 때 쓸 수 있는 필드 목록을 같이 낸다. 그걸 그대로
    돌려주는 것이 이 함수가 하는 일의 전부다.

    ``base.cached`` 를 그대로 물려받는다. 수리 시도가 캐시를 다시 쓰려면 접두사가
    바이트까지 같아야 하므로 새로 만들지 않는다.
    """
    return Prompt(
        cached=base.cached,
        fresh="\n".join(
            [
                base.fresh,
                "",
                "=== 직전 시도가 거부됐다 ===",
                "```sql",
                rejected_sql.strip(),
                "```",
                f"거부 이유: {error}",
                "",
                "위 이유를 고쳐서 SQL을 다시 쓴다. 같은 SQL을 반복하지 않는다.",
                "",
                "=== SQL ===",
            ]
        ),
    )


def valid_examples(
    examples: tuple[Example, ...],
    layer: LogicalLayer,
    graph: SchemaGraph,
    *,
    dialect: str = "postgres",
) -> tuple[Example, ...]:
    """이 레이어에서 실제로 확장되는 예시만 남긴다.

    검사가 아니라 **실행**이다. 필드 이름이 있는지만 보면 필터 전용 필드를
    `SELECT` 에 쓴 예시가 통과하는데, 그건 확장이 거부하는 SQL 이다. 모델에게
    거부당할 SQL 을 모범으로 보여 주는 셈이 된다.
    """
    kept: list[Example] = []
    for example in examples:
        try:
            expand(example.logical_sql, layer, graph, dialect=dialect)
        except (ExpansionError, Exception):  # noqa: BLE001 — 실패하면 그냥 뺀다
            continue
        kept.append(example)
    return tuple(kept)


# ── NL2SQL 골드셋 스키마용 예시 ────────────────────────────────────────────────
#
# 골드셋의 정답 SQL 은 물리 SQL 이라 그대로 예시로 쓸 수 없다 — 조인을 쓰지 말라고
# 해 놓고 조인이 든 모범을 보여 주게 된다. 그래서 같은 질문에 대한 **논리** SQL 을
# 여기 따로 적는다. 넷은 일부러 서로 다른 것을 가르친다:
#
#   * 차원 앵커로 팩트 둘을 나란히 놓기 (매출 대 계획)
#   * 사전집계된 자식의 차원에 조건 걸기 (계정 이름)
#   * 팩트 앵커로 전표 단위 상세 보기
#   * 0 분모를 막는 비율 계산
#
# 픽스처 ``fixtures/enterprise_bi.sql`` 를
# :func:`tablefold.t2sql.preset.fold_star_schema` 로 접었을 때의 필드 이름이다.
# 다른 스키마에서는 ``valid_examples`` 가 전부 뺀다.
NL2SQL_EXAMPLES: tuple[Example, ...] = (
    Example(
        question="2010년 7월 사업장별 매출 계획과 매출액, 달성률 알려줘",
        logical_sql="""SELECT HEAD_CD, HEAD_NM,
       SUM(f_sales_SALES_AMT_sum) AS SALE_ACT,
       SUM(f_mgmt_plans_SALES_PLAN_AMT_sum) AS SALE_PLAN,
       CASE WHEN SUM(f_mgmt_plans_SALES_PLAN_AMT_sum) = 0 THEN NULL
            ELSE SUM(f_sales_SALES_AMT_sum)
                 / SUM(f_mgmt_plans_SALES_PLAN_AMT_sum) * 100 END AS ACHIEVE_RATE
FROM D_SA_ORG
WHERE f_sales_YYYYMMDD LIKE '201007%'
  AND f_mgmt_plans_YYYYMMDD LIKE '201007%'
GROUP BY HEAD_CD, HEAD_NM""",
    ),
    Example(
        question="2010년 8월 재무조직별 매출액 계정의 손익금액 알려줘",
        logical_sql="""SELECT HEAD_NM, SUM(f_pls_AMT_sum) AS PL_AMT
FROM D_FI_ORG
WHERE f_pls_YYYYMM = '201008'
  AND f_pls_PL_ACCT2_NM = '매출액'
GROUP BY HEAD_NM""",
    ),
    Example(
        question="2010년 7월 3일 품목별 매출 상세 내역 보여줘",
        logical_sql="""SELECT YYYYMMDD, ITEM_NM, SUM(SALES_AMT) AS SALES_AMT
FROM F_SALES
WHERE YYYYMMDD = '20100703'
GROUP BY YYYYMMDD, ITEM_NM""",
    ),
    Example(
        question="2010년 7월 작업장별 생산 실적과 목표, 달성률 조회",
        logical_sql="""SELECT WORKSHOP_CD, WORKSHOP_NM,
       SUM(f_productions_PROD_ACTUAL_QTY_sum) AS PROD_ACT,
       SUM(f_productions_PROD_PLAN_QTY_sum) AS PROD_PLAN,
       CASE WHEN SUM(f_productions_PROD_PLAN_QTY_sum) = 0 THEN NULL
            ELSE SUM(f_productions_PROD_ACTUAL_QTY_sum)
                 / SUM(f_productions_PROD_PLAN_QTY_sum) * 100 END AS ACHIEVE_RATE
FROM D_WORKSHOP
WHERE f_productions_YYYYMMDD LIKE '201007%'
GROUP BY WORKSHOP_CD, WORKSHOP_NM""",
    ),
)
