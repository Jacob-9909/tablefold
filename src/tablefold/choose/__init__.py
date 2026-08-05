"""3. 어느 입도에서 물을지 정한다.

앵커는 모델이 앉을 입도다. 어느 테이블을 앵커로 삼느냐가 답할 수 있는 질문의
범위를 정한다.

``cost`` 가 여기 있는 이유는 선택이 후보의 *가격* 을 알아야 하기 때문이다. 무엇이
필드가 되는지의 규칙을 한 벌만 두어야 추정과 실제가 어긋나지 않는다 — ``build`` 가
같은 규칙을 읽는다.
"""

from tablefold.choose.classify import TableProfile, profile_tables
from tablefold.choose.cluster import Clustering, SubjectArea, cluster
from tablefold.choose.select import (
    ExplicitSelector,
    GreedySelector,
    LLMSelector,
    SelectionPolicy,
    Selector,
)

__all__ = [
    "Clustering",
    "ExplicitSelector",
    "GreedySelector",
    "LLMSelector",
    "SelectionPolicy",
    "Selector",
    "SubjectArea",
    "TableProfile",
    "cluster",
    "profile_tables",
]
