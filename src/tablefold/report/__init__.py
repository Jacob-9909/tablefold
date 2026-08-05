"""5. 내보내고 잰다.

셋 다 같은 레이어를 서로 다른 독자에게 보여 준다 — ``prompt`` 는 LLM 에게,
``lineage`` 는 화면에게, ``fidelity`` 는 판단하는 사람에게.

``fidelity`` 가 여기 있는 이유: 사람이 쓴 시맨틱 레이어와 달리 이 레이어는 자동으로
나오므로, 아무도 검증하지 않은 것을 LLM 에 주게 된다. 무엇을 잃었는지 재는 축이
없으면 "테이블을 버려서 비율을 좋게 만드는" 개선이 개선처럼 보인다.
"""

from tablefold.report import fidelity, lineage
from tablefold.report.llm import LLMUnavailable, anthropic_completer
from tablefold.report.prompt import (
    render_report,
    render_text,
    to_dict,
    to_json,
    to_yaml,
)

__all__ = [
    "LLMUnavailable",
    "anthropic_completer",
    "fidelity",
    "lineage",
    "render_report",
    "render_text",
    "to_dict",
    "to_json",
    "to_yaml",
]
