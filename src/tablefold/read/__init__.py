"""1. 물리 스키마를 읽는다.

DDL 텍스트든 살아 있는 데이터베이스 카탈로그든, 같은 :class:`PhysicalSchema` 를
낸다. 뒤 단계는 어디서 왔는지 몰라도 된다.
"""

from tablefold.read.base import Introspector
from tablefold.read.ddl import DDLIntrospector

__all__ = ["DDLIntrospector", "Introspector"]
