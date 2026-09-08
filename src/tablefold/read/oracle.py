"""Oracle 카탈로그에서 물리 스키마를 읽는다.

Postgres·SQL Server 탐색기와 같은 계약을 지킨다 — 테이블·컬럼·기본키·외래키를
읽어 :class:`PhysicalSchema` 하나로 조립할 뿐, 추론이나 폴딩은 하지 않는다.

Oracle 특유의 두 가지가 조립에 영향을 준다.

* **소유자(owner)가 곧 스키마다.** 그리고 대문자로 저장된다. 소문자로 넘기면
  카탈로그가 조용히 0행을 돌려주므로 — 접속은 됐는데 스키마가 비어 보인다 —
  :class:`OracleIntrospector` 가 올려서 조회한다.
* **행 수는 통계값이다.** ``all_tables.num_rows`` 는 마지막 통계 수집 시점의
  값이라 오래됐을 수 있고, 통계를 한 번도 돌리지 않았으면 ``NULL`` 이다.
  Postgres 의 ``reltuples`` 와 같은 성격이며 :mod:`tablefold.choose.classify`
  의 크기 가중치로만 쓰이므로 그 정도면 충분하다.
"""

from __future__ import annotations

from tablefold.ir import (
    ForeignKey,
    PhysicalColumn,
    PhysicalSchema,
    PhysicalTable,
)

DIALECT = "oracle"
"""이 소스가 말하는 sqlglot 방언. 읽는 쪽과 쓰는 쪽이 따로 적지 않도록 한 벌만 둔다."""

# 뷰까지 포함한다. Postgres 탐색기가 ``relkind IN ('r','p','v','m')`` 로 뷰와
# 구체화 뷰를 넣으므로, 여기서 테이블만 읽으면 같은 스키마를 두 드라이버로 읽었을
# 때 결과가 달라진다. ``all_tab_comments`` 가 둘 다 들고 있어 이걸 기준으로 삼고
# 행 수만 ``all_tables`` 에서 붙인다(뷰에는 없다).
_TABLES_SQL = """
SELECT
    tc.table_name,
    tc.comments,
    t.num_rows
FROM all_tab_comments tc
LEFT JOIN all_tables t
       ON t.owner = tc.owner
      AND t.table_name = tc.table_name
WHERE tc.owner = :owner
  AND tc.table_type IN ('TABLE', 'VIEW')
ORDER BY tc.table_name
"""

_COLUMNS_SQL = """
SELECT
    c.table_name,
    c.column_name,
    c.data_type,
    c.data_length,
    c.data_precision,
    c.data_scale,
    CASE WHEN c.nullable = 'Y' THEN 1 ELSE 0 END AS is_nullable,
    cc.comments,
    c.column_id
FROM all_tab_columns c
LEFT JOIN all_col_comments cc
       ON cc.owner = c.owner
      AND cc.table_name = c.table_name
      AND cc.column_name = c.column_name
WHERE c.owner = :owner
ORDER BY c.table_name, c.column_id
"""

_PRIMARY_KEYS_SQL = """
SELECT
    cols.table_name,
    cols.column_name,
    cols.position
FROM all_constraints cons
JOIN all_cons_columns cols
  ON cols.owner = cons.owner
 AND cols.constraint_name = cons.constraint_name
WHERE cons.constraint_type = 'P'
  AND cons.owner = :owner
ORDER BY cols.table_name, cols.position
"""

# 참조 쪽 컬럼은 ``r_constraint_name`` 이 가리키는 제약(대상 테이블의 PK/UNIQUE)
# 에서 가져온다. 두 쪽을 ``position`` 으로 맞춰야 복합키의 짝이 어긋나지 않는다.
_FOREIGN_KEYS_SQL = """
SELECT
    cons.constraint_name,
    src.table_name  AS from_table,
    src.column_name AS from_column,
    tgt.table_name  AS to_table,
    tgt.column_name AS to_column,
    src.position
FROM all_constraints cons
JOIN all_cons_columns src
  ON src.owner = cons.owner
 AND src.constraint_name = cons.constraint_name
JOIN all_cons_columns tgt
  ON tgt.owner = cons.r_owner
 AND tgt.constraint_name = cons.r_constraint_name
 AND tgt.position = src.position
WHERE cons.constraint_type = 'R'
  AND cons.owner = :owner
ORDER BY cons.constraint_name, src.position
"""


class OracleIntrospector:
    def __init__(self, dsn: str, *, schema: str = "") -> None:
        """*dsn* 은 python-oracledb 접속 문자열, *schema* 는 소유자다.

        *schema* 를 비우면 접속 계정 자신의 스키마를 읽는다 — Oracle 에서
        기본 스키마는 로그인 사용자이지, Postgres 의 ``public`` 처럼 고정된
        이름이 아니다.
        """
        self._dsn = dsn
        self._schema = schema

    def introspect(self) -> PhysicalSchema:
        try:
            import oracledb
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "Oracle introspection needs the 'oracle' extra: "
                "pip install 'tablefold[oracle]'"
            ) from exc

        with oracledb.connect(self._dsn) as conn, conn.cursor() as cur:
            owner = (self._schema or _current_schema(cur)).upper()
            params = {"owner": owner}

            cur.execute(_TABLES_SQL, params)
            table_rows = cur.fetchall()
            cur.execute(_COLUMNS_SQL, params)
            column_rows = cur.fetchall()
            cur.execute(_PRIMARY_KEYS_SQL, params)
            pk_rows = cur.fetchall()
            cur.execute(_FOREIGN_KEYS_SQL, params)
            fk_rows = cur.fetchall()

        return assemble(owner, table_rows, column_rows, pk_rows, fk_rows)


def _current_schema(cur) -> str:
    cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual")
    row = cur.fetchone()
    return row[0] if row and row[0] else ""


def render_type(
    name: str,
    length: int | None,
    precision: int | None,
    scale: int | None,
) -> str:
    """카탈로그의 조각난 타입 정보를 선언문 형태로 되돌린다.

    ``ir._norm_type`` 이 ``VARCHAR2(50)`` 같은 선언형을 파싱하도록 되어 있어서,
    카탈로그가 나눠 주는 길이·정밀도를 다시 붙여 주는 편이 IR 전체와 일관된다.

    ``NUMBER`` 는 정밀도가 ``NULL`` 일 수 있고(부동 소수점 형태), 그때 ``(None)``
    을 찍으면 파서가 깨지므로 이름만 남긴다.
    """
    upper = name.upper()
    if upper in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}:
        return f"{name}({length})" if length else name
    if upper == "NUMBER":
        if precision is None:
            return name
        return f"{name}({precision},{scale or 0})" if scale else f"{name}({precision})"
    if upper in {"FLOAT"} and precision is not None:
        return f"{name}({precision})"
    return name


def assemble(
    schema_name: str,
    table_rows: list[tuple],
    column_rows: list[tuple],
    pk_rows: list[tuple],
    fk_rows: list[tuple],
) -> PhysicalSchema:
    """카탈로그 행들을 :class:`PhysicalSchema` 하나로 조립한다."""
    columns: dict[str, list[PhysicalColumn]] = {}
    for tbl, col, ty, length, prec, scale, nullable, comment, _ in column_rows:
        columns.setdefault(tbl, []).append(
            PhysicalColumn(
                name=col,
                type=render_type(ty, length, prec, scale),
                nullable=bool(nullable),
                comment=comment,
            )
        )

    primary_keys: dict[str, list[str]] = {}
    for tbl, col, _ in pk_rows:
        primary_keys.setdefault(tbl, []).append(col)

    tables = tuple(
        PhysicalTable(
            name=tbl,
            columns=tuple(columns.get(tbl, ())),
            primary_key=tuple(primary_keys.get(tbl, ())),
            schema=schema_name,
            comment=comment,
            row_estimate=int(rows) if rows is not None else None,
        )
        for tbl, comment, rows in table_rows
        if columns.get(tbl)
    )

    grouped: dict[str, dict] = {}
    for name, from_table, from_column, to_table, to_column, _ in fk_rows:
        entry = grouped.setdefault(
            name,
            {
                "from_table": from_table,
                "to_table": to_table,
                "from_columns": [],
                "to_columns": [],
            },
        )
        entry["from_columns"].append(from_column)
        entry["to_columns"].append(to_column)

    foreign_keys = tuple(
        ForeignKey(
            name=name,
            from_table=e["from_table"],
            from_columns=tuple(e["from_columns"]),
            to_table=e["to_table"],
            to_columns=tuple(e["to_columns"]),
        )
        for name, e in grouped.items()
    )

    return PhysicalSchema(tables=tables, foreign_keys=foreign_keys)
