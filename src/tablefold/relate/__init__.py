"""2. 관계를 되찾는다.

웨어하우스는 외래 키를 잘 선언하지 않는다. 관계가 없으면 그래프가 없고, 그래프가
없으면 접을 수 없다. 이름 규칙과 기본 키로 후보를 만들고, 호출자가 실제 데이터로
검증한다.
"""

from tablefold.relate.graph import SchemaGraph, infer_foreign_keys
from tablefold.relate.keys import infer_from_primary_keys
from tablefold.relate.validate import (
    recover_with_data,
    unique_single_keys,
    validate_foreign_keys,
    violation_rate,
)

__all__ = [
    "SchemaGraph",
    "infer_foreign_keys",
    "infer_from_primary_keys",
    "recover_with_data",
    "unique_single_keys",
    "validate_foreign_keys",
    "violation_rate",
]
