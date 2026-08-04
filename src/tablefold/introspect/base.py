"""데이터베이스 탐색(Introspection) 인터페이스 규격.

탐색기(Introspector)의 유일한 역할은 명확하고 정확한
:class:`PhysicalSchema`를 생성하는 것입니다.
별도의 추론이나 폴딩 작업은 수행하지 않으며, 실시간 DB 연결이든
디스크의 DDL 파일이든 하위 단계는 항상 동일한 IR(중간 표현)을 읽습니다.
"""

from __future__ import annotations

from typing import Protocol

from tablefold.schema.ir import PhysicalSchema


class Introspector(Protocol):
    def introspect(self) -> PhysicalSchema:
        """이 정보원이 설명하는 물리 스키마를 반환합니다."""
        ...
