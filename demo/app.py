"""tablefold 프로덕션급 스키마 엔지니어링 대시보드 백엔드 서버."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from demo import live
from tablefold.choose.classify import profile_tables
from tablefold.choose.cluster import SelectionPolicy, build_lattice
from tablefold.choose.cost import DEFAULT_FIELD_BUDGET, MAX_MODEL_FIELDS
from tablefold.choose.select import ExplicitSelector
from tablefold.fold import fold
from tablefold.read.ddl import DDLIntrospector
from tablefold.relate.graph import SchemaGraph, infer_foreign_keys
from tablefold.relate.synthesize import add_period_anchor
from tablefold.report import compression as comp
from tablefold.report import fidelity as fid
from tablefold.report import information as info
from tablefold.report import lineage as lin
from tablefold.report import prompt as emit
from tablefold.rewrite.expand import ExpansionError, expand
from tablefold.t2sql.prepare import dialect_for_live
from tablefold.t2sql.preset import (
    STAR_FIELD_BUDGET,
    STAR_MAX_HOPS,
    STAR_MAX_MODEL_FIELDS,
    recover_relationships,
    split_anchors,
)

BASE_DIR = Path(__file__).parent.parent
FIXTURES_DIR = BASE_DIR / "fixtures"

app = FastAPI(title="Tablefold 2-Tier Schema Engineering Dashboard")

# 이 API 는 라이브 소스일 때 실제 데이터베이스 조회와 LLM 지출을 일으킨다.
# 와일드카드 + credentials 조합은 아무 웹페이지나 방문자의 브라우저를 통해
# 질의를 날릴 수 있게 한다 — 허용 출처를 명시한다. 환경 변수로 늘린다.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "TABLEFOLD_ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FoldRequest(BaseModel):
    ddl: str = ""
    source: str = "live"
    anchor_mode: str = "mixed"

    coverage: float = 0.90
    min_gain: int = 2
    max_cost: float = 10.0
    field_budget: int = DEFAULT_FIELD_BUDGET
    prompt_budget: int | None = None
    """레이어 전체가 쓸 **문자 수**. 주면 ``field_budget`` 을 대신한다.

    진짜 제약은 프롬프트 길이지 필드 수가 아니다. 필드 수는 대리 변수이고
    필드당 평균 50자였지만 이름·주석 길이에 따라 흔들린다.
    """

    max_areas: int | None = 6

    monthly_summaries: bool = False
    """팩트마다 월 입도 요약 가상 테이블을 세운다. 행이 진짜 줄어드는 압축."""


class ExpandRequest(BaseModel):
    ddl: str = ""
    sql: str
    source: str = "live"
    dialect: str = ""
    """비워 두면 소스가 정한다. :class:`ChatRequest` 와 같은 규칙이다."""

    anchor_mode: str = "mixed"
    coverage: float = 0.90
    min_gain: int = 2
    max_cost: float = 10.0
    field_budget: int = DEFAULT_FIELD_BUDGET
    prompt_budget: int | None = None
    """레이어 전체가 쓸 **문자 수**. 주면 ``field_budget`` 을 대신한다.

    진짜 제약은 프롬프트 길이지 필드 수가 아니다. 필드 수는 대리 변수이고
    필드당 평균 50자였지만 이름·주석 길이에 따라 흔들린다.
    """

    max_areas: int | None = 6


class ChatRequest(BaseModel):
    question: str
    ddl: str = ""
    source: str = "live"
    dialect: str = ""
    monthly_summaries: bool = False
    """팩트마다 월 입도 요약 모델을 세워 추세 질문이 요약을 읽게 한다."""
    """비워 두면 소스가 정한다 — 라이브는 MSSQL 이므로 ``tsql``.

    기본값이 ``postgres`` 이던 동안 화면은 ``LIMIT`` 을 만들어 MSSQL 에 던졌고
    "상위 10개" 질문이 전부 문법 오류로 죽었다. CLI 는 같은 질문에서
    ``OFFSET … FETCH NEXT`` 를 냈다.
    """
    anchor_mode: str = "mixed"
    coverage: float = 0.90
    min_gain: int = 2
    max_cost: float = 10.0
    field_budget: int = DEFAULT_FIELD_BUDGET
    prompt_budget: int | None = None
    """레이어 전체가 쓸 **문자 수**. 주면 ``field_budget`` 을 대신한다.

    진짜 제약은 프롬프트 길이지 필드 수가 아니다. 필드 수는 대리 변수이고
    필드당 평균 50자였지만 이름·주석 길이에 따라 흔들린다.
    """

    max_areas: int | None = 6


@app.get("/api/sample")
def get_sample_ddl() -> dict[str, str]:
    sample_file = FIXTURES_DIR / "enterprise_bi.sql"
    if not sample_file.exists():
        sample_file = FIXTURES_DIR / "retail_50.sql"
    return {"ddl": sample_file.read_text(encoding="utf-8")}


@app.get("/api/sources")
def list_sources() -> dict[str, Any]:
    """어떤 스키마 소스를 쓸 수 있는지."""
    return {
        "live_available": live.available(),
        "live_label": os.environ.get("TABLEFOLD_MSSQL_DB", "enterprise_bi"),
        "financial_available": live.available(),
        "financial_label": "금융합성데이터 (13테이블, 2508컬럼)",
    }


def _star_source(schema, meta: dict[str, Any]):
    """스타 프리셋 — 챗봇·CLI 가 쓰는 것과 **같은** 레이어."""
    schema = add_period_anchor(recover_relationships(schema))
    dimensions, facts = split_anchors(SchemaGraph.build(schema))
    anchors = facts + dimensions

    options: dict[str, Any] = {
        "infer_missing_keys": False,
        "include_aggregates": True,
        "expose_child_filters": True,
        "prefix_joined_fields": False,
        "max_hops": STAR_MAX_HOPS,
        "field_budget": STAR_FIELD_BUDGET,
        "max_model_fields": STAR_MAX_MODEL_FIELDS,
    }
    if anchors:
        options["selector"] = ExplicitSelector(anchors, prune_redundant=True)
        options["max_areas"] = len(anchors)
    return schema, options, {**meta, "anchors": list(anchors)}


def _load_source(req: FoldRequest | ExpandRequest):
    """요청이 가리키는 스키마와, 그 스키마에 맞는 폴드 옵션을 함께 돌려준다."""
    if req.source == "financial":
        try:
            schema, meta = live.load_financial()
        except live.LiveUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        anchors = (
            "member_info",
            "bank_receipt_product_info",
            "public_fund_product_info",
        )
        options = {
            "selector": ExplicitSelector(anchors, prune_redundant=True),
            "infer_missing_keys": True,
            "include_aggregates": True,
            "expose_child_filters": True,
            "prefix_joined_fields": False,
            "max_hops": 2,
            "field_budget": 5000,
            "max_model_fields": 1000,
        }
        return schema, options, meta

    if req.source == "live":
        try:
            schema, meta = live.load()
        except live.LiveUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if req.anchor_mode == "star":
            return _star_source(schema, meta)

        facts = tuple(meta["facts"])
        dims = tuple(meta["dimensions"])

        # 앵커를 무엇으로 잡느냐가 곧 무엇을 물을 수 있느냐다. 실측(NL2SQL 19테이블):
        #
        #   팩트 앵커  5,823자   답변가능 27.0%   — 팩트 간 질문을 못 푼다
        #   차원 앵커 16,172자   답변가능 94.4%   — 팩트를 사전집계해 나란히 놓는다
        #   혼합      21,846자   답변가능  100%   — 둘 다 두고 질문에 맞는 쪽을 쓴다
        #
        # 팩트 앵커의 27%는 추상적인 결함이 아니다. 골드셋 48문항 중 12문항이
        # "매출과 계획이 서로 다른 모델에 있어 답할 수 없다"로 거부됐고, 그 12건이
        # 정확히 이 분모와 분자의 차이다.
        # 혼합은 팩트와 차원을 그냥 합친 목록이라 절반이 낭비다. 실측(NL2SQL):
        # 팩트 앵커 10개 중 5개는 답할 수 있는 질문을 하나도 늘리지 않았다 —
        # 이미 차원 앵커가 같은 조합을 담고 있었다. 중복을 빼면 모델 19→8,
        # 프롬프트 19,002→12,388자, 답변가능률은 100% 그대로다.
        prune = False
        if req.anchor_mode == "mixed" and facts and dims:
            anchors, aggregates, filters = facts + dims, True, True
            prune = True
        elif req.anchor_mode == "dim" and dims:
            anchors, aggregates, filters = dims, True, True
        elif facts:
            anchors, aggregates, filters = facts, False, False
        else:
            anchors, aggregates, filters = (), True, False

        options = {
            "selector": (
                ExplicitSelector(anchors, prune_redundant=prune) if anchors else None
            ),
            "infer_missing_keys": False,
            "include_aggregates": aggregates,
            "expose_child_filters": filters,
            "prefix_joined_fields": False,
            "max_hops": 2,
        }
        if anchors:
            # 앵커를 이름으로 지목했으면 개수 상한은 그 개수다. 화면의 기본값 6이
            # 19개를 지목한 요청을 조용히 6개로 자르면, 답변가능률이 100%에서
            # 13.5%로 떨어지는데 화면에는 아무 설명도 남지 않는다.
            options["max_areas"] = len(anchors)
            # 혼합 모드는 예산과 무관하게 334필드로 자족한다 (중복 앵커를 뺐기
            # 때문이다). 예산은 dim / fact 모드가 2단 필터 때문에 5만 자까지
            # 부푸는 것을 막는 자리다.
            options["field_budget"] = 400
            # 모델당 상한이 진짜 병목이었다. 64 로 두면 D_FI_ORG 처럼 12개 표를
            # 안는 모델에서 F_PL 이 통째로 잘려 나가고, 재무 주제의 질문이 답이
            # 안 된다. 주제표 9개 기준으로 64→4개, 120→6개, 200→7개가 풀린다.
            options["max_model_fields"] = 200
        return schema, options, meta

    if not req.ddl.strip():
        raise HTTPException(status_code=400, detail="DDL SQL content cannot be empty.")
    schema = DDLIntrospector(req.ddl).introspect()
    if not schema.tables:
        raise HTTPException(
            status_code=400, detail="No valid CREATE TABLE statements found in DDL."
        )
    if req.anchor_mode == "star":
        return _star_source(schema, {})
    return schema, {"infer_missing_keys": True}, {}


@app.post("/api/fold")
def run_fold(req: FoldRequest) -> dict[str, Any]:
    try:
        raw_schema, fold_options, source_meta = _load_source(req)
        baseline_ddl = source_meta.pop("ddl", None) or req.ddl

        # 1. 외래키 추론 — 라이브 소스는 이미 데이터로 검증된 키를 들고 온다.
        if fold_options.get("infer_missing_keys"):
            inferred = infer_foreign_keys(raw_schema)
            declared_only = raw_schema.foreign_keys
            enriched_schema = (
                raw_schema.with_foreign_keys(raw_schema.foreign_keys + inferred)
                if inferred
                else raw_schema
            )
        else:
            # 라이브 소스는 이미 데이터로 검증된 키를 붙여서 온다. 그 키들은
            # 데이터베이스에 *선언된* 것이 아니라 되찾은 것이므로 추론 쪽으로만
            # 센다. 양쪽에 다 넣으면 같은 관계가 두 번 세어져, 화면의 "AI가 직접
            # 이어붙여야 하는 표"가 22 대신 44 로 나온다.
            inferred = tuple(fk for fk in raw_schema.foreign_keys if fk.inferred)
            declared_only = tuple(
                fk for fk in raw_schema.foreign_keys if not fk.inferred
            )
            enriched_schema = raw_schema

        # 2-1. 월별 요약 — 행이 진짜 줄어드는 압축. 요약은 무조건 **독립
        #      앵커**로 살려 둔다. 흡수해 버리면 월 입도가 filter_only 로만
        #      남아 "월별 추세"를 GROUP BY 할 수단이 사라진다. greedy 가 고른
        #      앵커는 시험 접기로 알아낸 뒤 요약을 나란히 붙여 최종 폴드한다.
        if req.monthly_summaries:
            from tablefold.relate.synthesize import add_monthly_summaries

            widened, built = add_monthly_summaries(enriched_schema)
            if built:
                existing_selector = fold_options.get("selector")
                if existing_selector is not None:
                    anchors = (*existing_selector.anchors, *built)
                    fold_options["selector"] = ExplicitSelector(
                        anchors, prune_redundant=True
                    )
                    fold_options["max_areas"] = len(anchors)
                    enriched_schema = widened
                else:
                    trial_policy = SelectionPolicy(
                        coverage_target=req.coverage,
                        min_gain=req.min_gain,
                        max_fields_per_table=req.max_cost,
                        max_areas=fold_options.pop("max_areas", req.max_areas),
                    )
                    trial = fold(
                        widened,
                        policy=trial_policy,
                        field_budget=fold_options.get("field_budget", req.field_budget),
                        prompt_budget=req.prompt_budget,
                        max_model_fields=fold_options.get(
                            "max_model_fields", MAX_MODEL_FIELDS
                        ),
                        max_hops=fold_options.get("max_hops", 3),
                        include_aggregates=fold_options.get("include_aggregates", True),
                        expose_child_filters=fold_options.get(
                            "expose_child_filters", False
                        ),
                        infer_missing_keys=False,
                    )
                    greedy_anchors = tuple(m.base_table for m in trial.layer.models)
                    fold_options["selector"] = ExplicitSelector(
                        (*greedy_anchors, *built), prune_redundant=False
                    )
                    fold_options["max_areas"] = len(greedy_anchors) + len(built)
                    enriched_schema = widened

        # 2. 그래프 구축 및 프로파일링 (Fact Score)
        graph = SchemaGraph.build(enriched_schema)
        profiles = profile_tables(graph)

        max_areas = fold_options.pop("max_areas", req.max_areas)
        field_budget = fold_options.pop("field_budget", req.field_budget)
        model_cap = fold_options.pop("max_model_fields", MAX_MODEL_FIELDS)
        # 요청이 문자 예산을 주면 그것이 이긴다. 필드 수는 대리 변수다.
        prompt_budget = req.prompt_budget
        policy = SelectionPolicy(
            coverage_target=req.coverage,
            min_gain=req.min_gain,
            max_fields_per_table=req.max_cost,
            max_areas=max_areas,
        )

        # 3. 후보 격자 (Candidate Lattice) 수치 측정
        #
        # **폴드가 실제로 쓸 값과 같아야 한다.** 여기에 숫자를 따로 적으면 화면의
        # 가격표가 실제로 지불한 가격이 아니게 된다 — ``max_hops=3``,
        # ``max_fields=64`` 로 값을 매겨 놓고 폴드는 ``max_hops=2``,
        # ``max_model_fields=200`` 으로 돌던 동안, 격자가 비싸다고 표시한 앵커가
        # 실제로는 싸게 들어왔고 그 반대도 있었다.
        lattice = build_lattice(
            graph,
            profiles,
            max_hops=fold_options.get("max_hops", 3),
            max_fields=model_cap,
            include_aggregates=fold_options.get("include_aggregates", True),
            expose_child_filters=fold_options.get("expose_child_filters", False),
        )

        # 4. 폴딩 실행 (Tier-1 Core Models)
        result = fold(
            enriched_schema,
            policy=policy,
            field_budget=field_budget,
            prompt_budget=prompt_budget,
            max_model_fields=model_cap,
            **fold_options,
        )

        layer_dict = emit.to_dict(result.layer)

        # 5. 2-Tier Architecture 구성 (Tier-1 Core Models vs Tier-2 Edge Tables)
        all_physical_tables = {t.name.lower(): t for t in raw_schema.tables}
        covered_table_names: set[str] = set()

        for model in result.layer.models:
            covered_table_names.add(model.base_table.lower())
            for absorbed in model.absorbed_tables:
                covered_table_names.add(absorbed.lower())

        tier2_edge_table_names = [
            name for name in all_physical_tables if name not in covered_table_names
        ]

        tier2_tables_detailed = [
            {
                "name": all_physical_tables[name].name,
                "column_count": len(all_physical_tables[name].columns),
                "primary_key": list(all_physical_tables[name].primary_key),
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type,
                        "nullable": c.nullable,
                    }
                    for c in all_physical_tables[name].columns
                ],
            }
            for name in tier2_edge_table_names
        ]

        # 6. 물리 스키마 상세 메타데이터 정리
        profile_map = {p.name.lower(): p for p in profiles}

        physical_tables_detailed = []
        for t in raw_schema.tables:
            p = profile_map.get(t.name.lower())
            physical_tables_detailed.append(
                {
                    "name": t.name,
                    "column_count": len(t.columns),
                    "columns": [
                        {
                            "name": c.name,
                            "type": c.type,
                            "nullable": c.nullable,
                            "is_numeric": c.is_numeric,
                            "is_temporal": c.is_temporal,
                        }
                        for c in t.columns
                    ],
                    "primary_key": list(t.primary_key),
                    "row_estimate": t.row_estimate,
                    "fact_score": round(p.score, 4) if p else 0.0,
                    "role": p.role.value if p else "dimension",
                    "in_degree": p.in_degree if p else 0,
                    "out_degree": p.out_degree if p else 0,
                    "tier": "Tier-1 (Core)"
                    if t.name.lower() in covered_table_names
                    else "Tier-2 (Edge)",
                }
            )

        # 7. 추론된 외래키 상세
        inferred_fks_detailed = [
            {
                "from_table": fk.from_table,
                "from_columns": list(fk.from_columns),
                "to_table": fk.to_table,
                "to_columns": list(fk.to_columns),
                "confidence": fk.confidence,
            }
            for fk in inferred
        ]

        # 8. 후보 격자 상세 수치
        candidates_detailed = [
            {
                "name": c.name,
                "role": c.role,
                "score": round(c.score, 4),
                "reach_count": len(c.reach),
                "reach_tables": sorted(list(c.reach)),
                "estimated_fields": c.estimated_fields,
                "fields_per_table": round(c.fields_per_table, 2),
            }
            for c in lattice.candidates
        ]

        # 9. 2-Tier Structured Prompt Text Generation
        core_prompt_text = emit.render_text(result.layer)
        edge_prompt_lines = [
            "",
            "--- TIER-2 EDGE TABLES (On-Demand / Specific Query Fallback) ---",
        ]
        for t in tier2_tables_detailed:
            col_list_str = ", ".join(f"{c['name']} {c['type']}" for c in t["columns"])
            edge_prompt_lines.append(f"TABLE {t['name']} ({col_list_str})")

        full_2tier_prompt_text = core_prompt_text + "\n".join(edge_prompt_lines)

        # 10. 반영도 — 얼마나 줄었나(위의 size)와 짝을 이루는 "무엇을 잃었나".
        #     둘 중 하나만 보이면 테이블을 버려서 좋아지는 지표가 된다.
        measured = fid.measure(
            result.layer, result.graph, merged_aliases=result.merged_aliases
        )

        # 11. 계보 — 화면이 ERD 처럼 그릴 수 있게 노드/엣지로 뒤집은 같은 사실.
        lineage_graph = lin.to_graph(
            result.layer,
            result.graph,
            profiles={
                p.name.lower(): {
                    "score": p.score,
                    "role": p.role.value,
                    "in_degree": p.in_degree,
                    "out_degree": p.out_degree,
                }
                for p in profiles
            },
            criteria={
                "coverage_target": req.coverage,
                "min_gain": req.min_gain,
                "max_fields_per_table": req.max_cost,
                "field_budget": field_budget,
                "prompt_budget": prompt_budget,
                "max_areas": max_areas,
                "anchor_mode": req.anchor_mode,
            },
        )

        return {
            "fidelity": fid.to_dict(measured),
            "fidelity_report": fid.render_report(measured),
            # 컬럼이 어디서 와서 어떻게 접혔는지 — 모델 상세의 흐름도가 읽는다.
            "compression": comp.measure(result.layer),
            # 커서가 없는 오프라인 소스도 복제율은 계산된다. 비트 보존율은
            # measured 플래그가 거짓일 뿐, 화면은 그에 맞춰 문구를 고른다.
            "information": info.measure(result.layer, result.schema),
            "lineage": lineage_graph,
            "tier_summary": {
                "tier1_core_models_count": len(result.layer.models),
                "tier1_covered_physical_tables_count": len(covered_table_names),
                "tier2_edge_tables_count": len(tier2_edge_table_names),
                "total_physical_tables_count": len(raw_schema.tables),
            },
            # 화면 상단의 "무엇이 줄었나"를 정직하게 표시하기 위한 실측값.
            # 모델 수가 아니라 실제로 프롬프트에 들어가는 글자 수가 기준이다.
            "size": {
                "ddl_chars": len(baseline_ddl),
                "core_prompt_chars": len(core_prompt_text),
                "full_prompt_chars": len(full_2tier_prompt_text),
                "total_columns": sum(len(t.columns) for t in raw_schema.tables),
                "total_fields": result.layer.field_count,
            },
            "physical": {
                "table_count": len(raw_schema.tables),
                "total_columns": sum(len(t.columns) for t in raw_schema.tables),
                "declared_fk_count": len(declared_only),
                "inferred_fk_count": len(inferred),
                "inferred_fks": inferred_fks_detailed,
                "tables": physical_tables_detailed,
            },
            "tier2_edge_tables": tier2_tables_detailed,
            "analytics": {
                "candidates_count": len(lattice.candidates),
                "candidates": candidates_detailed,
            },
            "source": {
                "kind": req.source,
                "anchor_mode": req.anchor_mode,
                **source_meta,
            },
            "logical": layer_dict,
            "prompt_text": full_2tier_prompt_text,
            "core_prompt_text": core_prompt_text,
            "report": emit.render_report(result.layer),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/expand")
def run_expand(req: ExpandRequest) -> dict[str, Any]:
    if not req.sql.strip():
        raise HTTPException(status_code=400, detail="SQL query is required.")

    try:
        schema, fold_options, _ = _load_source(req)
        max_areas = fold_options.pop("max_areas", req.max_areas)
        field_budget = fold_options.pop("field_budget", req.field_budget)
        model_cap = fold_options.pop("max_model_fields", MAX_MODEL_FIELDS)
        # 요청이 문자 예산을 주면 그것이 이긴다. 필드 수는 대리 변수다.
        prompt_budget = req.prompt_budget
        policy = SelectionPolicy(
            coverage_target=req.coverage,
            min_gain=req.min_gain,
            max_fields_per_table=req.max_cost,
            max_areas=max_areas,
        )
        # ``model_cap`` 은 위에서 이미 꺼냈다. 여기서 한 번 더 ``pop`` 하면 키가
        # 없으므로 기본값 64 로 덮여서, ``/api/fold`` 가 그린 레이어(200)와 다른
        # 레이어를 상대로 확장하게 된다 — 화면에 보이는 필드가 "모르는 필드"로
        # 거부되는 경로였다.
        result = fold(
            schema,
            policy=policy,
            field_budget=field_budget,
            prompt_budget=prompt_budget,
            max_model_fields=model_cap,
            **fold_options,
        )

        # 화면의 "바꿔보기"도 같은 데이터베이스를 겨냥한다. 여기만 기본값
        # ``postgres`` 로 두면 챗봇과 다른 SQL 을 보여 주게 된다.
        expansion = expand(
            req.sql,
            result.layer,
            result.graph,
            dialect=dialect_for_live(req.dialect),
        )

        return {
            "expanded_sql": expansion.sql,
            "fields_used": list(expansion.fields_used),
            "joins_emitted": expansion.joins_emitted,
            "joins_available": expansion.joins_available,
            "joins_pruned": expansion.joins_pruned,
        }
    except ExpansionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/chat-capability")
def chat_capability() -> dict[str, bool]:
    """챗봇이 실제로 답할 수 있는지.

    키가 없으면 챗봇은 첫 질문에서 400 으로 끝난다. 눌러 봐야 실패하는 입력창을
    주는 것보다, 화면이 미리 알게 해서 안내를 보여 주고 입력을 막는 편이 낫다.
    호출이 아니라 생성자만 지어 보므로 네트워크를 쓰지 않는다.
    """
    from tablefold.t2sql.provider import ProviderUnavailable, default_completer

    try:
        default_completer()
    except ProviderUnavailable:
        return {"llm_available": False}
    return {"llm_available": True}


def _resolve_chat_dialect(source: str, requested: str) -> str:
    """챗봇 SQL 을 실제로 뽑아 낼 방언.

    라이브 소스는 접속 대상이 MSSQL 이라 :func:`dialect_for_live` 가 정한다.
    예제(DDL) 소스는 데이터베이스가 없으므로 PostgreSQL 이 기본이다 — 치환을
    그대로 적용했던 동안 화면은 "PostgreSQL 문법으로 답합니다"라고 안내하는데
    T-SQL 이 나왔다. 안내와 결과가 어긋나면 안내가 거짓말이 된다.
    """
    if source in ("live", "financial"):
        return dialect_for_live(requested)
    return requested or "postgres"


class AutoTuneRequest(BaseModel):
    source: str = "ddl"
    ddl: str = ""


GOLDSET_GLOB = "*.xlsx"
"""프로젝트 루트에서 골드셋을 찾을 패턴. 없으면 곡선 기능이 조용히 꺼진다."""


def _find_goldset():
    """읽히는 첫 골드셋과 그 케이스. 없으면 ``(None, ())``.

    골드셋이 없는 설치도 있으므로 실패를 예외로 만들지 않는다 — 곡선 버튼이
    안 보일 뿐 나머지는 그대로 돈다.
    """
    from tablefold.t2sql.goldset import load_goldset

    for path in sorted(BASE_DIR.glob(GOLDSET_GLOB)):
        try:
            cases = load_goldset(path)
        except Exception:  # noqa: BLE001 — 엑셀은 형태가 제각각이다
            continue
        if any(c.gold_references for c in cases):
            return path, cases
    return None, ()


@app.get("/api/goldset")
def goldset_info() -> dict[str, Any]:
    path, cases = _find_goldset()
    return {
        "available": path is not None,
        "name": path.name if path else "",
        "cases": len(cases),
        "subjects": len({c.subject for c in cases}),
    }


@app.post("/api/curve")
def run_curve(req: FoldRequest) -> dict[str, Any]:
    """프롬프트 길이 ↔ 답할 수 있는 질문 수의 **교환곡선**.

    설정 하나를 추천하는 대신 곡선을 준다. 노브가 존재하는 이유는 목적함수가
    없어서인데, 골드셋이 목적함수가 되면 "얼마를 줄까"가 튜닝이 아니라 비용 대
    커버리지의 결정이 된다. LLM 을 부르지 않으므로 한 점당 폴드 한 번이다.
    """
    _, cases = _find_goldset()
    if not cases:
        raise HTTPException(status_code=404, detail="골드셋을 찾지 못했다.")

    from tablefold.choose import tune as tuning

    try:
        schema, _, _ = _load_source(req)
        points = tuning.curve(schema, cases)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    best = tuning.knee(points)
    return {
        "points": [
            {
                "prompt_budget": p.prompt_budget,
                "prompt_length": p.prompt_length,
                "models": p.models,
                "fields": p.fields,
                "answered": p.answered,
                "total": p.total,
                "answered_subjects": p.answered_subjects,
                "total_subjects": p.total_subjects,
            }
            for p in points
        ],
        "knee": best.prompt_budget if best else None,
    }


@app.post("/api/autotune")
def run_autotune(req: AutoTuneRequest):
    if not req.ddl.strip() and req.source == "ddl":
        default_file = FIXTURES_DIR / "enterprise_bi.sql"
        if default_file.exists():
            req.ddl = default_file.read_text(encoding="utf-8")
        else:
            sample_file = FIXTURES_DIR / "retail_50.sql"
            req.ddl = sample_file.read_text(encoding="utf-8")

    try:
        fold_req = FoldRequest(source=req.source, ddl=req.ddl)
        schema, _, _ = _load_source(fold_req)

        from tablefold.choose.autotune import autotune_stream

        def event_generator():
            import json

            for item in autotune_stream(schema):
                # ``_`` 로 시작하는 키는 파이썬 호출자용이다(``AutoTuneResult``
                # 객체). JSON 으로 나가면 안 된다.
                payload = {k: v for k, v in item.items() if not k.startswith("_")}
                yield json.dumps(payload) + "\n"

        return StreamingResponse(event_generator(), media_type="application/x-ndjson")

    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/chat")
def run_chat(req: ChatRequest) -> dict[str, Any]:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question is required.")

    # Load schema based on request or default enterprise_bi fixture if empty
    if not req.ddl.strip() and req.source == "ddl":
        default_file = FIXTURES_DIR / "enterprise_bi.sql"
        if default_file.exists():
            req.ddl = default_file.read_text(encoding="utf-8")
        else:
            sample_file = FIXTURES_DIR / "retail_50.sql"
            req.ddl = sample_file.read_text(encoding="utf-8")

    try:
        schema, _, _ = _load_source(req)
        inferred = tuple(fk for fk in schema.foreign_keys if fk.inferred)

        from tablefold.t2sql import (
            NL2SQL_EXAMPLES,
            TextToSQLEngine,
            default_completer,
            prepare_for_questions,
        )

        prep = prepare_for_questions(
            schema,
            already_recovered=len(inferred) if req.source == "live" else 0,
            # 화면이 문자 예산을 정했으면 챗봇도 **같은 레이어**를 봐야 한다.
            # 여기만 기본값으로 두면 화면에 그려진 필드를 챗봇이 모른다.
            prompt_budget=req.prompt_budget,
            # 추세 질문은 요약 모델이 읽는다 — 라우터 카탈로그에 우선 힌트가
            # 붙는다(:func:`tablefold.t2sql.prompt.render_catalog`).
            monthly_summaries=req.monthly_summaries,
        )

        engine = TextToSQLEngine(
            prep.result,
            completer=default_completer(),
            examples=NL2SQL_EXAMPLES,
            # 만든 SQL 을 그대로 이 데이터베이스에 실행한다. 생성 방언과 실행
            # 대상이 어긋나면 문법 오류로 죽는다.
            dialect=_resolve_chat_dialect(req.source, req.dialect),
        )

        gen_res = engine.generate(req.question)
        exec_res = live.execute_query(gen_res.physical_sql)

        return {
            "question": gen_res.question,
            "logical_sql": gen_res.logical_sql,
            "physical_sql": gen_res.physical_sql,
            "models_used": list(gen_res.models_used),
            "fields_used": list(gen_res.fields_used),
            "joins_emitted": gen_res.joins_emitted,
            "joins_available": gen_res.joins_available,
            "joins_pruned": gen_res.joins_pruned,
            "repairs": gen_res.repairs,
            "attempts_count": len(gen_res.attempts),
            "preparation_note": prep.note,
            "routed_to": gen_res.routed_to,
            "rerouted_from": gen_res.rerouted_from,
            "source_kind": req.source,
            "execution_result": exec_res,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


static_path = Path(__file__).parent / "static"


@app.get("/chat")
def get_chat_page():
    chat_file = static_path / "chat.html"
    if chat_file.exists():
        return FileResponse(chat_file)
    raise HTTPException(status_code=404, detail="Chat page not found.")


if static_path.exists():
    app.mount("/", StaticFiles(directory=static_path, html=True), name="static")
