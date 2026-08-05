from __future__ import annotations

import sys

from tablefold.clustering import select
from tablefold.clustering.cluster import Clustering, SubjectArea, cluster
from tablefold.clustering.select import LLMSelector, SelectionPolicy, Selector
from tablefold.composition.compose import ComposeOptions, compose
from tablefold.expansion.expand import ExpansionError, expand

# `tablefold.graph` 는 아래에서 서브모듈로 치환되므로, 그 패키지 안의 다른 모듈은
# 치환 전에 가져와 여기서 재노출한다.
from tablefold.graph.from_keys import infer_from_primary_keys  # noqa: E402
from tablefold.graph.graph import SchemaGraph, infer_foreign_keys
from tablefold.pipeline.pipeline import FoldResult, fold
from tablefold.presentation.cost import DEFAULT_FIELD_BUDGET, estimate_fields
from tablefold.presentation.emit import (
    render_report,
    render_text,
    to_dict,
    to_json,
    to_yaml,
)

# Import in dependency order to prevent circular initialization
from tablefold.schema.ir import (
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
from tablefold.scoring.classify import TableProfile, profile_tables

# Legacy module aliases for backward compatibility
sys.modules["tablefold.ir"] = sys.modules["tablefold.schema.ir"]
sys.modules["tablefold.graph"] = sys.modules["tablefold.graph.graph"]
sys.modules["tablefold.classify"] = sys.modules["tablefold.scoring.classify"]
sys.modules["tablefold.cluster"] = sys.modules["tablefold.clustering.cluster"]
sys.modules["tablefold.compose"] = sys.modules["tablefold.composition.compose"]
sys.modules["tablefold.expand"] = sys.modules["tablefold.expansion.expand"]
sys.modules["tablefold.select"] = select
sys.modules["tablefold.pipeline.select"] = select
sys.modules["tablefold.emit"] = sys.modules["tablefold.presentation.emit"]
sys.modules["tablefold.cost"] = sys.modules["tablefold.presentation.cost"]
sys.modules["tablefold.llm"] = sys.modules["tablefold.presentation.llm"]

__all__ = [
    "Cardinality",
    "Clustering",
    "ComposeOptions",
    "DEFAULT_FIELD_BUDGET",
    "ExpansionError",
    "FieldKind",
    "FoldResult",
    "ForeignKey",
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
    "infer_from_primary_keys",
    "cluster",
    "compose",
    "estimate_fields",
    "expand",
    "fold",
    "infer_foreign_keys",
    "profile_tables",
    "render_report",
    "render_text",
    "to_dict",
    "to_json",
    "to_yaml",
]
