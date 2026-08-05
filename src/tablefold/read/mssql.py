"""SQL Server 카탈로그에서 물리 스키마를 읽는다.

Postgres 탐색기와 같은 계약을 지킨다 — 테이블·컬럼·기본키·외래키를 읽어
:class:`PhysicalSchema` 하나로 조립할 뿐, 추론이나 폴딩은 하지 않는다.

Postgres와 다른 점은 행 수 추정의 출처다. SQL Server에는 ``reltuples`` 같은
플래너 통계가 없으므로 ``sys.partitions``의 실제 행 수를 쓴다. 이 값은 추정이
아니라 실측이고, 그만큼 :mod:`tablefold.choose.classify`의 크기 신호가 정확해진다.
"""

from __future__ import annotations

from tablefold.ir import (
    ForeignKey,
    PhysicalColumn,
    PhysicalSchema,
    PhysicalTable,
)

_TABLES_SQL = """
SELECT t.name,
       CAST(ep.value AS NVARCHAR(MAX)) AS table_comment,
       SUM(p.rows) AS row_count
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
LEFT JOIN sys.extended_properties ep
       ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
WHERE s.name = {p}
GROUP BY t.name, CAST(ep.value AS NVARCHAR(MAX))
ORDER BY t.name
"""

_COLUMNS_SQL = """
SELECT t.name AS table_name,
       c.name AS column_name,
       ty.name AS data_type,
       c.max_length, c.precision, c.scale, c.is_nullable,
       CAST(ep.value AS NVARCHAR(MAX)) AS column_comment,
       c.column_id
FROM sys.columns c
JOIN sys.tables t   ON t.object_id = c.object_id
JOIN sys.schemas s  ON s.schema_id = t.schema_id
JOIN sys.types ty   ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.extended_properties ep
       ON ep.major_id = c.object_id AND ep.minor_id = c.column_id
      AND ep.name = 'MS_Description'
WHERE s.name = {p}
ORDER BY t.name, c.column_id
"""

_PRIMARY_KEYS_SQL = """
SELECT t.name AS table_name, c.name AS column_name, ic.key_ordinal
FROM sys.key_constraints k
JOIN sys.tables t        ON t.object_id = k.parent_object_id
JOIN sys.schemas s       ON s.schema_id = t.schema_id
JOIN sys.index_columns ic
       ON ic.object_id = k.parent_object_id AND ic.index_id = k.unique_index_id
JOIN sys.columns c       ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE k.type = 'PK' AND s.name = {p}
ORDER BY t.name, ic.key_ordinal
"""

_FOREIGN_KEYS_SQL = """
SELECT fk.name,
       src.name AS from_table, src_col.name AS from_column,
       tgt.name AS to_table,   tgt_col.name AS to_column,
       fkc.constraint_column_id
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables src      ON src.object_id = fk.parent_object_id
JOIN sys.schemas s       ON s.schema_id = src.schema_id
JOIN sys.tables tgt      ON tgt.object_id = fk.referenced_object_id
JOIN sys.columns src_col ON src_col.object_id = fkc.parent_object_id
                        AND src_col.column_id = fkc.parent_column_id
JOIN sys.columns tgt_col ON tgt_col.object_id = fkc.referenced_object_id
                        AND tgt_col.column_id = fkc.referenced_column_id
WHERE s.name = {p}
ORDER BY fk.name, fkc.constraint_column_id
"""


class MSSQLIntrospector:
    """``pyodbc`` 또는 ``pymssql`` 연결로 SQL Server 스키마를 읽는다.

    ``connect`` 는 DB-API 연결을 돌려주는 호출 가능 객체다. 드라이버를 직접
    import 하지 않는 이유는 환경마다 설치되는 드라이버가 다르기 때문이고,
    덕분에 이 모듈은 테스트에서 가짜 연결로도 돌릴 수 있다.
    """

    def __init__(self, connect, *, schema: str = "dbo", paramstyle: str = "auto") -> None:
        self._connect = connect
        self._schema = schema
        self._paramstyle = paramstyle

    def introspect(self) -> PhysicalSchema:
        with self._connect() as conn:
            placeholder = _placeholder(conn, self._paramstyle)
            cur = conn.cursor()
            rows = []
            for sql in (
                _TABLES_SQL,
                _COLUMNS_SQL,
                _PRIMARY_KEYS_SQL,
                _FOREIGN_KEYS_SQL,
            ):
                cur.execute(sql.format(p=placeholder), (self._schema,))
                rows.append(cur.fetchall())

        return assemble(self._schema, *rows)


def _placeholder(conn, paramstyle: str) -> str:
    """드라이버가 쓰는 파라미터 표시자를 고른다.

    ``pyodbc`` 는 ``?`` 를, ``pymssql`` 은 ``%s`` 를 쓴다. DB-API 모듈은
    ``paramstyle`` 을 공개하므로 연결 객체의 모듈에서 되짚는다. 알아내지
    못하면 ``?`` 로 둔다 — SQL Server 드라이버에서 더 흔한 쪽이다.
    """
    if paramstyle == "auto":
        import sys

        module = sys.modules.get(type(conn).__module__)
        root = sys.modules.get(type(conn).__module__.split(".")[0])
        paramstyle = getattr(module, "paramstyle", None) or getattr(
            root, "paramstyle", "qmark"
        )
    return "%s" if paramstyle in {"format", "pyformat"} else "?"


def render_type(name: str, max_length: int, precision: int, scale: int) -> str:
    """카탈로그의 조각난 타입 정보를 선언문 형태로 되돌린다.

    ``ir._norm_type`` 이 ``VARCHAR(50)`` 같은 선언형을 파싱하도록 되어 있어서,
    카탈로그가 나눠 주는 길이·정밀도를 다시 붙여 주는 편이 IR 전체와 일관된다.
    """
    lowered = name.lower()
    if lowered in {"varchar", "char", "varbinary", "binary"}:
        return f"{name}(max)" if max_length == -1 else f"{name}({max_length})"
    if lowered in {"nvarchar", "nchar"}:
        # nchar/nvarchar 의 max_length 는 바이트 수라 문자 수로 되돌린다.
        return f"{name}(max)" if max_length == -1 else f"{name}({max_length // 2})"
    if lowered in {"decimal", "numeric"}:
        return f"{name}({precision},{scale})"
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
    for tbl, col, ty, max_len, prec, scale, nullable, comment, _ in column_rows:
        columns.setdefault(tbl, []).append(
            PhysicalColumn(
                name=col,
                type=render_type(ty, max_len, prec, scale),
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
