"""Text-to-SQL — 질문을 받아 실행 가능한 물리 SQL을 낸다.

접힌 레이어(:mod:`tablefold.fold`)가 "SQL 을 쓰는 데 필요한 표와 컬럼"이라면,
이 패키지는 그 정보를 읽고 **완성된 SQL 을 내는 쪽**이다.

```python
from tablefold.read.ddl import DDLIntrospector
from tablefold.t2sql import TextToSQLEngine, NL2SQL_EXAMPLES, fold_star_schema

schema = DDLIntrospector.from_path("fixtures/enterprise_bi.sql").introspect()
engine = TextToSQLEngine(fold_star_schema(schema), examples=NL2SQL_EXAMPLES)
print(engine.generate("2010년 7월 사업장별 매출과 계획 알려줘").physical_sql)
```
"""

from tablefold.t2sql.engine import (
    Attempt,
    GenerationError,
    GenerationResult,
    TextToSQLEngine,
    generate_sql,
)
from tablefold.t2sql.goldset import GoldCase, load_goldset, schema_gap
from tablefold.t2sql.parse import SQLNotFound, extract_sql
from tablefold.t2sql.prepare import Preparation, prepare_for_questions
from tablefold.t2sql.preset import (
    fold_star_schema,
    recover_relationships,
    split_anchors,
)
from tablefold.t2sql.prompt import (
    NL2SQL_EXAMPLES,
    Example,
    Prompt,
    build_prompt,
    build_repair_prompt,
    build_router_prompt,
    parse_model_name,
    render_catalog,
    valid_examples,
)
from tablefold.t2sql.provider import (
    Completer,
    ProviderUnavailable,
    Usage,
    anthropic_completer,
    default_completer,
    openai_completer,
)

__all__ = [
    "Attempt",
    "Completer",
    "Example",
    "GenerationError",
    "GenerationResult",
    "GoldCase",
    "NL2SQL_EXAMPLES",
    "Preparation",
    "Prompt",
    "ProviderUnavailable",
    "SQLNotFound",
    "TextToSQLEngine",
    "Usage",
    "anthropic_completer",
    "build_prompt",
    "build_repair_prompt",
    "build_router_prompt",
    "default_completer",
    "extract_sql",
    "fold_star_schema",
    "generate_sql",
    "load_goldset",
    "openai_completer",
    "parse_model_name",
    "prepare_for_questions",
    "recover_relationships",
    "render_catalog",
    "schema_gap",
    "split_anchors",
    "valid_examples",
]

