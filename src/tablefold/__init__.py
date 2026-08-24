"""tablefold — 물리 스키마를 소수의 넓은 논리 모델로 접고, 다시 펼친다.

패키지는 파이프라인 순서대로 놓여 있다. 위에서 아래로 읽으면 실행 순서다:

    ir.py        모든 단계가 주고받는 공통 타입 (물리 / 논리 두 층)

    read/        1. 물리 스키마를 읽는다         base ddl mssql postgres
    relate/      2. 관계를 되찾는다              graph keys
    choose/      3. 어느 입도에서 물을지 정한다   classify cost cluster select
    build/       4. 논리 모델을 만든다           compose
    report/      5. 내보내고 잰다                prompt lineage fidelity llm
    rewrite/     6. 논리 SQL 을 물리 SQL 로      expand

    fold.py      전체 배선. 단계 순서가 코드로 적혀 있는 유일한 자리
    cli.py       명령줄

폴더 이름과 파일 이름을 일부러 다르게 두었다. ``expansion/expand.py`` 처럼 겹치면
경로가 같은 말을 두 번 하고, 읽는 쪽은 어느 쪽이 뜻을 담은 이름인지 매번 다시
판단해야 한다.

한때 이 파일이 ``sys.modules`` 를 덮어써서 짧은 별칭 11개를 만들었다. 같은 것을 두
이름으로 부르게 됐고, 더 나쁘게는 ``tablefold.graph`` 패키지가 그 안의 ``graph.py``
로 치환되어 옆에 있는 ``from_keys`` 를 import 할 수 없었다. 이제 짧은 이름이 곧
정식 경로다.
"""

from __future__ import annotations

from tablefold.build.compose import ComposeOptions, compose
from tablefold.choose import select
from tablefold.choose.classify import TableProfile, profile_tables
from tablefold.choose.cluster import Clustering, SubjectArea, cluster
from tablefold.choose.cost import DEFAULT_FIELD_BUDGET, estimate_fields
from tablefold.choose.select import LLMSelector, SelectionPolicy, Selector
from tablefold.fold import FoldResult, fold
from tablefold.ir import (
    Cardinality,
    FieldKind,
    ForeignKey,
    JoinStep,
    LogicalField,
    LogicalLayer,
    LogicalModel,
    PhysicalColumn,
    PhysicalSchema,
    PhysicalTable,
)
from tablefold.relate.graph import SchemaGraph, infer_foreign_keys
from tablefold.relate.keys import infer_from_primary_keys
from tablefold.report.prompt import (
    render_report,
    render_text,
    to_dict,
    to_json,
    to_yaml,
)
from tablefold.rewrite.expand import ExpansionError, expand
from tablefold.t2sql import GenerationResult, TextToSQLEngine, generate_sql

__all__ = [
    "DEFAULT_FIELD_BUDGET",
    "Cardinality",
    "Clustering",
    "ComposeOptions",
    "ExpansionError",
    "FieldKind",
    "FoldResult",
    "ForeignKey",
    "GenerationResult",
    "JoinStep",
    "LLMSelector",
    "LogicalField",
    "LogicalLayer",
    "LogicalModel",
    "PhysicalColumn",
    "PhysicalSchema",
    "PhysicalTable",
    "SchemaGraph",
    "SelectionPolicy",
    "Selector",
    "SubjectArea",
    "TableProfile",
    "TextToSQLEngine",
    "cluster",
    "compose",
    "estimate_fields",
    "expand",
    "fold",
    "generate_sql",
    "infer_foreign_keys",
    "infer_from_primary_keys",
    "profile_tables",
    "render_report",
    "render_text",
    "select",
    "to_dict",
    "to_json",
    "to_yaml",
]
