"""tablefold 프로덕션급 스키마 엔지니어링 대시보드 백엔드 서버."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
from tablefold.report import fidelity as fid
from tablefold.report import lineage as lin
from tablefold.report import prompt as emit
from tablefold.rewrite.expand import ExpansionError, expand

BASE_DIR = Path(__file__).parent.parent
FIXTURES_DIR = BASE_DIR / "fixtures"

app = FastAPI(title="Tablefold 2-Tier Schema Engineering Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FoldRequest(BaseModel):
    ddl: str = ""
    source: str = "ddl"
    """``ddl`` 이면 위의 텍스트를, ``live`` 면 접속된 데이터베이스를 읽는다."""

    anchor_mode: str = "auto"
    """``auto`` 는 탐욕적 집합 커버. 스타 스키마에서는 ``fact`` / ``dim`` 으로
    앵커를 직접 지정한다 — 팩트가 앵커면 차원이 인라인되고, 차원이 앵커면
    팩트들이 사전집계되어 팩트 간 조인이 사라진다."""

    coverage: float = 0.90
    min_gain: int = 2
    max_cost: float = 10.0
    field_budget: int = DEFAULT_FIELD_BUDGET
    """레이어 전체가 쓸 수 있는 필드 수. 모델 하나당이 아니다."""

    max_areas: int | None = 6


class ExpandRequest(BaseModel):
    ddl: str = ""
    sql: str
    source: str = "ddl"
    anchor_mode: str = "auto"
    coverage: float = 0.90
    min_gain: int = 2
    max_cost: float = 10.0
    field_budget: int = DEFAULT_FIELD_BUDGET
    max_areas: int | None = 6


@app.get("/api/sample")
def get_sample_ddl() -> dict[str, str]:
    sample_file = FIXTURES_DIR / "retail_50.sql"
    if not sample_file.exists():
        raise HTTPException(status_code=404, detail="Sample DDL fixture not found.")
    return {"ddl": sample_file.read_text(encoding="utf-8")}


@app.get("/api/sources")
def list_sources() -> dict[str, Any]:
    """어떤 스키마 소스를 쓸 수 있는지."""
    return {
        "live_available": live.available(),
        "live_label": os.environ.get("TABLEFOLD_MSSQL_DB", ""),
    }


def _load_source(req: FoldRequest | ExpandRequest):
    """요청이 가리키는 스키마와, 그 스키마에 맞는 폴드 옵션을 함께 돌려준다.

    옵션이 소스마다 다른 이유는 스키마의 성격이 다르기 때문이다. 웨어하우스
    스타 스키마는 앵커가 자명하고(팩트가 곧 앵커), 테이블 이름이 사람이 읽을
    말이 아니라서 인라인 필드에 접두사를 붙이면 실제 질의와 어긋난다.
    """
    if req.source == "live":
        try:
            schema, meta = live.load()
        except live.LiveUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

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

        # 2. 그래프 구축 및 프로파일링 (Fact Score)
        graph = SchemaGraph.build(enriched_schema)
        profiles = profile_tables(graph)

        max_areas = fold_options.pop("max_areas", req.max_areas)
        field_budget = fold_options.pop("field_budget", req.field_budget)
        model_cap = fold_options.pop("max_model_fields", MAX_MODEL_FIELDS)
        policy = SelectionPolicy(
            coverage_target=req.coverage,
            min_gain=req.min_gain,
            max_fields_per_table=req.max_cost,
            max_areas=max_areas,
        )

        # 3. 후보 격자 (Candidate Lattice) 수치 측정
        # 후보 가격 산정은 모델 하나의 상한 기준. 레이어 예산과는 다른 값이다.
        lattice = build_lattice(graph, profiles, max_hops=3, max_fields=MAX_MODEL_FIELDS)

        # 4. 폴딩 실행 (Tier-1 Core Models)
        result = fold(
            enriched_schema,
            policy=policy,
            field_budget=field_budget,
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
                    "tier": "Tier-1 (Core)" if t.name.lower() in covered_table_names else "Tier-2 (Edge)",
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
        measured = fid.measure(result.layer, result.graph)

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
                "max_areas": max_areas,
                "anchor_mode": req.anchor_mode,
            },
        )

        return {
            "fidelity": fid.to_dict(measured),
            "fidelity_report": fid.render_report(measured),
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
            "source": {"kind": req.source, "anchor_mode": req.anchor_mode, **source_meta},
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
        policy = SelectionPolicy(
            coverage_target=req.coverage,
            min_gain=req.min_gain,
            max_fields_per_table=req.max_cost,
            max_areas=max_areas,
        )
        model_cap = fold_options.pop("max_model_fields", MAX_MODEL_FIELDS)
        result = fold(
            schema,
            policy=policy,
            field_budget=field_budget,
            max_model_fields=model_cap,
            **fold_options,
        )

        expansion = expand(req.sql, result.layer, result.graph)

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


static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/", StaticFiles(directory=static_path, html=True), name="static")
