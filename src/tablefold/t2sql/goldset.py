"""NL2SQL 골드셋 엑셀을 읽는다.

시트 하나가 케이스 하나이고, 배치가 고정되어 있다:

```
B2  * 골드셋 ID : SA_0002
B3  2. 선택월까지 사업장(영업조직)별 목표, 실적, 달성률 조회(누계)   ← 질문
C3  2010년 7월의 사업장별 …                                ← 값이 박힌 질문 (있으면)
B4  : Query
B5  SELECT … FROM F_SALES A LEFT OUTER JOIN D_SA_ORG B …   ← 정답 SQL (T-SQL)
C5  SELECT … FROM F_SALES A LEFT JOIN …                    ← 정답 SQL (PostgreSQL)
B6+ : 기타 / 주석
```

메타 시트는 케이스가 아니다. 이름으로 거르지 않고 **모양으로** 거른다 — 시트가
늘거나 이름이 바뀌어도 로더가 안 깨진다.

정답 SQL 은 물리 SQL 이라 채점의 기준이 되지 못한다. 우리 엔진은 논리 SQL 을 쓰고
확장하므로 문자열이 같을 수가 없다. 대신 정답이 **어느 물리 테이블을 읽는지**를
뽑아 둔다. 그건 비교할 수 있는 사실이고, "이 질문에 답하려면 이 표들이 한 모델에
있어야 한다"를 그대로 재는 값이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ``* 골드셋 ID : SA_0002`` 에서 ID 만.
_ID = re.compile(r"골드셋\s*ID\s*[:：]\s*([A-Za-z0-9_]+)")

# 정답 SQL 에서 물리 테이블 이름. FROM / JOIN 뒤에 오는 식별자를 줍는다.
_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)

# 테이블과 그 별칭. ``FROM F_SALES A`` · ``JOIN D_SA_ORG AS B``.
_TABLE_ALIAS = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?([A-Za-z][A-Za-z0-9_]*)?",
    re.IGNORECASE,
)

# ``A.SALES_AMT`` 같은 한정된 컬럼 참조.
_QUALIFIED = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")

# 별칭 자리에 올 수 있지만 별칭이 아닌 낱말. 안 거르면 ``FROM F_SALES WHERE`` 의
# ``WHERE`` 가 별칭으로 잡힌다.
_NOT_AN_ALIAS = frozenset(
    {
        "where", "group", "order", "having", "on", "left", "right", "inner",
        "outer", "full", "cross", "join", "union", "and", "or", "select",
        "limit", "offset", "with", "as",
    }
)

# 정답 SQL 이 별칭으로 쓰는 한 글자짜리 이름과, 테이블이 아닌 낱말.
_NOT_A_TABLE = frozenset({"select", "dual", "lateral", "unnest", "values"})


@dataclass(frozen=True)
class GoldCase:
    """골드셋 한 건."""

    case_id: str
    question: str
    concrete_question: str | None
    """값(연월, 조직명)이 박힌 질문. 있으면 평가에는 이쪽이 낫다 — 원본 질문은
    화면 메뉴 제목이라 "선택월"처럼 값이 비어 있다."""

    gold_sql: tuple[str, ...]
    """정답 SQL. 방언별로 여러 개일 수 있다(B5=T-SQL, C5=PostgreSQL)."""

    notes: tuple[str, ...]

    @property
    def subject(self) -> str:
        """``SA_0002`` 의 ``SA`` — 업무 주제."""
        return self.case_id.split("_", 1)[0]

    @property
    def asked(self) -> str:
        """평가에 쓸 질문."""
        return self.concrete_question or self.question

    @property
    def gold_tables(self) -> frozenset[str]:
        """정답 SQL 이 읽는 물리 테이블 이름(소문자)."""
        found: set[str] = set()
        for sql in self.gold_sql:
            for name in _TABLE.findall(sql):
                lowered = name.lower()
                if lowered not in _NOT_A_TABLE and len(lowered) > 1:
                    found.add(lowered)
        return frozenset(found)

    @property
    def gold_references(self) -> dict[str, frozenset[str]]:
        """정답 SQL 이 읽는 ``표 -> {컬럼}`` (전부 소문자).

        별칭을 되짚어야 한다 — 정답은 ``FROM F_SALES A … A.SALES_AMT`` 로 쓴다.
        되짚지 못한 한정자는 버린다. 서브쿼리가 만든 이름(``X.SALE_ACT``)은 물리
        컬럼이 아니고, 그걸 결손으로 세면 없는 결손이 생긴다.
        """
        found: dict[str, set[str]] = {}
        for sql in self.gold_sql:
            by_alias: dict[str, str] = {}
            for table, alias in _TABLE_ALIAS.findall(sql):
                lowered = table.lower()
                if lowered in _NOT_A_TABLE or len(lowered) <= 1:
                    continue
                by_alias[lowered] = lowered
                if alias and alias.lower() not in _NOT_AN_ALIAS:
                    by_alias[alias.lower()] = lowered
            for alias, column in _QUALIFIED.findall(sql):
                table = by_alias.get(alias.lower())
                if table is not None:
                    found.setdefault(table, set()).add(column.lower())
        return {k: frozenset(v) for k, v in found.items()}


def schema_gap(
    cases: tuple[GoldCase, ...], schema
) -> dict[str, frozenset[str]]:
    """정답 SQL 이 읽는데 스키마에 없는 ``표 -> {컬럼}``.

    답할 수 없는 케이스의 원인이 레이어인지 **스키마 자체**인지를 가른다. 스키마에
    컬럼이 없으면 어떤 폴드 설정으로도 답이 안 나온다 — 앵커를 바꿔 볼 일이 아니다.
    """
    known = {
        table.name.lower(): {c.name.lower() for c in table.columns}
        for table in schema.tables
    }
    missing: dict[str, set[str]] = {}
    for case in cases:
        for table, columns in case.gold_references.items():
            if table not in known:
                continue
            absent = columns - known[table]
            if absent:
                missing.setdefault(table, set()).update(absent)
    return {k: frozenset(v) for k, v in sorted(missing.items())}


def load_goldset(path: str | Path) -> tuple[GoldCase, ...]:
    """*path* 의 엑셀에서 케이스를 읽는다. 시트 순서를 지킨다."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 선택 의존성
        raise RuntimeError("openpyxl 이 필요하다: uv add --dev openpyxl") from exc

    book = openpyxl.load_workbook(Path(path), data_only=True, read_only=True)
    try:
        return tuple(
            case
            for sheet in book.worksheets
            if (case := _read_sheet(sheet)) is not None
        )
    finally:
        book.close()


def _read_sheet(sheet) -> GoldCase | None:
    """케이스 시트면 읽고, 아니면 ``None``.

    ``read_only`` 모드에서는 임의 좌표 접근이 느리므로 앞쪽 몇 줄만 읽어서 쓴다.
    """
    rows = []
    for index, row in enumerate(sheet.iter_rows(max_col=4, values_only=True), start=1):
        rows.append(row)
        if index >= 12:
            break

    def cell(row: int, col: int) -> str | None:
        if row > len(rows):
            return None
        values = rows[row - 1]
        if col > len(values) or values[col - 1] is None:
            return None
        text = str(values[col - 1]).strip()
        return text or None

    # 케이스 시트의 표지: B2 에 골드셋 ID, B3 에 질문.
    marker = cell(2, 2)
    question = cell(3, 2)
    if not marker or not question:
        return None
    found = _ID.search(marker)
    if not found:
        return None

    gold = tuple(
        sql for column in (2, 3, 4) if (sql := cell(5, column)) and _looks_like_sql(sql)
    )
    notes = tuple(
        note
        for row in range(6, min(len(rows), 12) + 1)
        if (note := cell(row, 2)) and not _looks_like_sql(note)
    )

    return GoldCase(
        case_id=found.group(1),
        question=question,
        # C3 과 C4 둘 다에서 값이 박힌 질문이 관측된다.
        concrete_question=_concrete(cell(3, 3), cell(4, 3)),
        gold_sql=gold,
        notes=notes,
    )


def _concrete(*candidates: str | None) -> str | None:
    for candidate in candidates:
        if candidate and not _looks_like_sql(candidate) and len(candidate) > 5:
            return candidate
    return None


def _looks_like_sql(text: str) -> bool:
    upper = text.upper()
    return "SELECT" in upper and "FROM" in upper
