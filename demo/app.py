"""tablefold 프로덕션급 스키마 엔지니어링 대시보드 백엔드 서버."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tablefold import emit
from tablefold.clustering.cluster import build_lattice, SelectionPolicy
from tablefold.expansion.expand import ExpansionError, expand
from tablefold.graph.graph import SchemaGraph, infer_foreign_keys
from tablefold.introspect.ddl import DDLIntrospector
from tablefold.pipeline.pipeline import fold
from tablefold.presentation.cost import DEFAULT_FIELD_BUDGET, MAX_MODEL_FIELDS
from tablefold.scoring.classify import profile_tables

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
    ddl: str
    coverage: float = 0.90
    min_gain: int = 2
    max_cost: float = 10.0
    field_budget: int = DEFAULT_FIELD_BUDGET
    """레이어 전체가 쓸 수 있는 필드 수. 모델 하나당이 아니다."""

    max_areas: int | None = 6


class ExpandRequest(BaseModel):
    ddl: str
    sql: str
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


@app.post("/api/fold")
def run_fold(req: FoldRequest) -> dict[str, Any]:
    if not req.ddl.strip():
        raise HTTPException(status_code=400, detail="DDL SQL content cannot be empty.")

    try:
        introspector = DDLIntrospector(req.ddl)
        raw_schema = introspector.introspect()
        if not raw_schema.tables:
            raise HTTPException(
                status_code=400, detail="No valid CREATE TABLE statements found in DDL."
            )

        # 1. 외래키 추론
        inferred = infer_foreign_keys(raw_schema)
        enriched_schema = (
            raw_schema.with_foreign_keys(raw_schema.foreign_keys + inferred)
            if inferred
            else raw_schema
        )

        # 2. 그래프 구축 및 프로파일링 (Fact Score)
        graph = SchemaGraph.build(enriched_schema)
        profiles = profile_tables(graph)

        policy = SelectionPolicy(
            coverage_target=req.coverage,
            min_gain=req.min_gain,
            max_fields_per_table=req.max_cost,
            max_areas=req.max_areas,
        )

        # 3. 후보 격자 (Candidate Lattice) 수치 측정
        # 후보 가격 산정은 모델 하나의 상한 기준. 레이어 예산과는 다른 값이다.
        lattice = build_lattice(graph, profiles, max_hops=3, max_fields=MAX_MODEL_FIELDS)

        # 4. 폴딩 실행 (Tier-1 Core Models)
        result = fold(
            raw_schema,
            policy=policy,
            field_budget=req.field_budget,
            infer_missing_keys=True,
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

        return {
            "tier_summary": {
                "tier1_core_models_count": len(result.layer.models),
                "tier1_covered_physical_tables_count": len(covered_table_names),
                "tier2_edge_tables_count": len(tier2_edge_table_names),
                "total_physical_tables_count": len(raw_schema.tables),
            },
            # 화면 상단의 "무엇이 줄었나"를 정직하게 표시하기 위한 실측값.
            # 모델 수가 아니라 실제로 프롬프트에 들어가는 글자 수가 기준이다.
            "size": {
                "ddl_chars": len(req.ddl),
                "core_prompt_chars": len(core_prompt_text),
                "full_prompt_chars": len(full_2tier_prompt_text),
                "total_columns": sum(len(t.columns) for t in raw_schema.tables),
                "total_fields": result.layer.field_count,
            },
            "physical": {
                "table_count": len(raw_schema.tables),
                "total_columns": sum(len(t.columns) for t in raw_schema.tables),
                "declared_fk_count": len(raw_schema.foreign_keys),
                "inferred_fk_count": len(inferred),
                "inferred_fks": inferred_fks_detailed,
                "tables": physical_tables_detailed,
            },
            "tier2_edge_tables": tier2_tables_detailed,
            "analytics": {
                "candidates_count": len(lattice.candidates),
                "candidates": candidates_detailed,
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
    if not req.ddl.strip() or not req.sql.strip():
        raise HTTPException(status_code=400, detail="DDL and SQL query are required.")

    try:
        introspector = DDLIntrospector(req.ddl)
        schema = introspector.introspect()
        policy = SelectionPolicy(
            coverage_target=req.coverage,
            min_gain=req.min_gain,
            max_fields_per_table=req.max_cost,
            max_areas=req.max_areas,
        )
        result = fold(schema, policy=policy, field_budget=req.field_budget)

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
