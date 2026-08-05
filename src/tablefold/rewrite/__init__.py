"""6. 논리 SQL 을 물리 SQL 로.

번역기가 아니라 검사기다. 논리 모델은 계약이고, 계약을 문서로만 적어 두면 지켜지지
않는다.
"""

from tablefold.rewrite.expand import ExpansionError, ExpansionResult, expand

__all__ = ["ExpansionError", "ExpansionResult", "expand"]
