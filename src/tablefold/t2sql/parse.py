"""완성 텍스트에서 SQL만 꺼낸다.

"SQL 만 돌려줘"는 요청이지 보장이 아니다. 모델은 앞에 설명을 붙이고, 뒤에
"이 쿼리는 …" 을 달고, 코드 펜스를 빠뜨린다. 여기서 그것을 걷어낸다.

꺼낸 결과는 **파서로 확인한다.** 눈으로 그럴듯한 문자열을 그대로 넘기면 실패가
:mod:`tablefold.rewrite.expand` 의 "could not parse SQL" 로 나타나는데, 그 메시지는
모델이 잘못 썼다는 뜻과 우리가 잘못 잘랐다는 뜻을 구별해 주지 못한다.
"""

from __future__ import annotations

import re

import sqlglot

_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# SQL 이 시작되는 자리. ``WITH`` 는 질의가 자체 CTE 를 들고 올 때 나온다.
_OPENERS = ("SELECT", "WITH")


class SQLNotFound(ValueError):
    """완성 텍스트에 파싱 가능한 SQL이 없다."""


def extract_sql(response: str, *, dialect: str = "postgres") -> str:
    """*response* 안의 SQL을 돌려준다. 없으면 :class:`SQLNotFound`.

    후보를 순서대로 시도하고, **파싱되는 첫 번째** 를 쓴다:

    1. 코드 펜스 안의 내용 (여러 개면 파싱되는 첫 번째)
    2. 첫 ``SELECT`` / ``WITH`` 부터 끝까지
    3. 그 후보에서 꼬리를 한 줄씩 떼어 가며 재시도 — 모델이 SQL 뒤에 설명을
       붙였을 때 잘리는 자리를 찾는다
    """
    text = response.strip()
    if not text:
        raise SQLNotFound("완성 텍스트가 비어 있다")

    for candidate in _candidates(text):
        parsed = _first_parsable(candidate, dialect)
        if parsed is not None:
            return parsed

    raise SQLNotFound(
        "완성 텍스트에서 파싱 가능한 SQL을 찾지 못했다: " f"{text[:200]}…"
    )


def _candidates(text: str) -> list[str]:
    found = [block.strip() for block in _FENCE.findall(text) if block.strip()]

    upper = text.upper()
    starts = [upper.find(opener) for opener in _OPENERS]
    start = min((s for s in starts if s >= 0), default=-1)
    if start >= 0:
        found.append(text[start:].strip())

    return found


def _first_parsable(candidate: str, dialect: str) -> str | None:
    """*candidate* 에서 꼬리를 줄여 가며 파싱되는 가장 긴 조각을 찾는다.

    앞이 아니라 뒤를 줄인다. 모델이 덧붙이는 산문은 SQL *뒤* 에 오고, 앞을
    줄이면 ``SELECT`` 를 잘라 먹어 뜻이 다른 질의가 통과할 수 있다.
    """
    lines = candidate.splitlines()
    while lines:
        chunk = "\n".join(lines).strip().rstrip(";").strip()
        if chunk and _parses(chunk, dialect):
            return chunk
        lines.pop()
    return None


def _parses(sql: str, dialect: str) -> bool:
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — 파싱 실패의 종류는 여기서 중요하지 않다
        return False
    # ``sqlglot`` 은 맨 낱말도 컬럼 하나짜리 식으로 파싱한다. 질의만 받는다.
    return isinstance(parsed, (sqlglot.exp.Select, sqlglot.exp.Union))
