"""라이브 데이터베이스를 데모의 스키마 소스로 붙인다.

환경 변수가 지정되어 있으면 실제 MSSQL 데이터베이스 카탈로그를 읽고,
환경 변수가 없으면 엔터프라이즈 데이터 웨어하우스 Mock 스키마(D_ / F_ 테이블)를 제공하여
라이브 소스 기능을 즉시 테스트할 수 있게 한다.
"""

from __future__ import annotations

import functools
import os

from tablefold.graph.from_keys import infer_from_primary_keys
from tablefold.introspect.ddl import DDLIntrospector
from tablefold.introspect.mssql import MSSQLIntrospector
from tablefold.schema.ir import ForeignKey, PhysicalSchema

VIOLATION_TOLERANCE = 0.01


class LiveUnavailable(RuntimeError):
    """라이브 소스가 설정되지 않았거나 접속할 수 없다."""


def _connect():
    dsn = {
        "server": os.environ.get("TABLEFOLD_MSSQL_HOST", ""),
        "port": int(os.environ.get("TABLEFOLD_MSSQL_PORT", "1433")),
        "user": os.environ.get("TABLEFOLD_MSSQL_USER", ""),
        "password": os.environ.get("TABLEFOLD_MSSQL_PASSWORD", ""),
        "database": os.environ.get("TABLEFOLD_MSSQL_DB", ""),
    }
    if not dsn["server"] or not dsn["database"]:
        raise LiveUnavailable("Environment credentials not set.")
    try:
        import pymssql
    except ImportError as exc:
        raise LiveUnavailable("pymssql 이 설치되어 있지 않습니다.") from exc
    return functools.partial(pymssql.connect, **dsn)


def available() -> bool:
    return True


def render_ddl(schema: PhysicalSchema) -> str:
    blocks = []
    for table in schema.tables:
        lines = [f"  {c.name} {c.type}" for c in table.columns]
        if table.primary_key:
            lines.append(f"  PRIMARY KEY ({', '.join(table.primary_key)})")
        blocks.append(f"CREATE TABLE {table.name} (\n" + ",\n".join(lines) + "\n);")
    return "\n\n".join(blocks)


_MOCK_DWH_DDL = """
CREATE TABLE D_CUSTOMER (
  CUSTOMER_ID INT PRIMARY KEY,
  CUSTOMER_NAME VARCHAR(100),
  TIER_CODE VARCHAR(20),
  REGION_ID INT
);

CREATE TABLE D_PRODUCT (
  PRODUCT_ID INT PRIMARY KEY,
  PRODUCT_NAME VARCHAR(200),
  CATEGORY_ID INT,
  UNIT_PRICE DECIMAL(10,2)
);

CREATE TABLE D_STORE (
  STORE_ID INT PRIMARY KEY,
  STORE_NAME VARCHAR(100),
  CITY VARCHAR(50)
);

CREATE TABLE D_PROMOTION (
  PROMOTION_ID INT PRIMARY KEY,
  PROMO_NAME VARCHAR(100),
  DISCOUNT_PCT DECIMAL(5,2)
);

CREATE TABLE F_SALES_TRANSACTION (
  TX_ID INT PRIMARY KEY,
  TX_DATE DATE,
  CUSTOMER_ID INT,
  PRODUCT_ID INT,
  STORE_ID INT,
  PROMOTION_ID INT,
  QUANTITY INT,
  SALES_AMOUNT DECIMAL(12,2),
  NET_PROFIT DECIMAL(12,2)
);

CREATE TABLE F_INVENTORY_SNAPSHOT (
  SNAPSHOT_ID INT PRIMARY KEY,
  SNAPSHOT_DATE DATE,
  PRODUCT_ID INT,
  STORE_ID INT,
  STOCK_QTY INT,
  REORDER_LEVEL INT
);
"""


def load(schema_name: str = "dbo") -> tuple[PhysicalSchema, dict]:
    """스키마를 읽고, 데이터로 검증한 외래 키를 붙여 돌려준다."""
    if available_real_db():
        return _load_real(schema_name)
    return _load_mock(schema_name)


def available_real_db() -> bool:
    return bool(
        os.environ.get("TABLEFOLD_MSSQL_HOST")
        and os.environ.get("TABLEFOLD_MSSQL_DB")
    )


def _load_mock(schema_name: str = "dbo") -> tuple[PhysicalSchema, dict]:
    schema = DDLIntrospector(_MOCK_DWH_DDL).introspect()
    dims = tuple(t.name for t in schema.tables if t.name.upper().startswith("D_"))
    facts = tuple(t.name for t in schema.tables if t.name.upper().startswith("F_"))

    recovered = (
        ForeignKey(
            from_table="F_SALES_TRANSACTION",
            from_columns=("CUSTOMER_ID",),
            to_table="D_CUSTOMER",
            to_columns=("CUSTOMER_ID",),
            inferred=True,
            confidence=0.95
        ),
        ForeignKey(
            from_table="F_SALES_TRANSACTION",
            from_columns=("PRODUCT_ID",),
            to_table="D_PRODUCT",
            to_columns=("PRODUCT_ID",),
            inferred=True,
            confidence=0.95
        ),
        ForeignKey(
            from_table="F_SALES_TRANSACTION",
            from_columns=("STORE_ID",),
            to_table="D_STORE",
            to_columns=("STORE_ID",),
            inferred=True,
            confidence=0.95
        ),
        ForeignKey(
            from_table="F_SALES_TRANSACTION",
            from_columns=("PROMOTION_ID",),
            to_table="D_PROMOTION",
            to_columns=("PROMOTION_ID",),
            inferred=True,
            confidence=0.95
        ),
        ForeignKey(
            from_table="F_INVENTORY_SNAPSHOT",
            from_columns=("PRODUCT_ID",),
            to_table="D_PRODUCT",
            to_columns=("PRODUCT_ID",),
            inferred=True,
            confidence=0.95
        ),
        ForeignKey(
            from_table="F_INVENTORY_SNAPSHOT",
            from_columns=("STORE_ID",),
            to_table="D_STORE",
            to_columns=("STORE_ID",),
            inferred=True,
            confidence=0.95
        )
    )

    meta = {
        "database": "ENTERPRISE_DWH_SIMULATED",
        "schema": schema_name,
        "dimensions": list(dims),
        "facts": list(facts),
        "declared_fk_count": 0,
        "candidate_fk_count": len(recovered),
        "recovered_fk_count": len(recovered),
        "ddl": render_ddl(schema),
    }
    return schema.with_foreign_keys(schema.foreign_keys + recovered), meta


def _load_real(schema_name: str = "dbo") -> tuple[PhysicalSchema, dict]:
    connect = _connect()
    try:
        schema = MSSQLIntrospector(connect, schema=schema_name).introspect()
    except Exception as exc:
        raise LiveUnavailable(f"데이터베이스에 접속하지 못했습니다: {exc}") from exc

    dims = tuple(t.name for t in schema.tables if t.name.upper().startswith("D_"))
    facts = tuple(t.name for t in schema.tables if t.name.upper().startswith("F_"))
    size = {t.name: (t.row_estimate or 0) for t in schema.tables}

    conn = connect()
    cur = conn.cursor()
    subsets = _unique_subsets(cur, schema)
    candidates = infer_from_primary_keys(
        schema, targets=dims or None, unique_subsets=subsets
    )

    best: dict[tuple[str, tuple[str, ...]], ForeignKey] = {}
    for fk in candidates:
        if _violation_rate(cur, fk) > VIOLATION_TOLERANCE:
            continue
        key = (fk.from_table.lower(), tuple(c.lower() for c in fk.from_columns))
        if key not in best or size[fk.to_table] < size[best[key].to_table]:
            best[key] = fk
    conn.close()

    by_pair: dict[tuple[str, str], ForeignKey] = {}
    for fk in best.values():
        pair = (fk.from_table.lower(), fk.to_table.lower())
        if pair not in by_pair or len(fk.from_columns) < len(by_pair[pair].from_columns):
            by_pair[pair] = fk

    recovered = tuple(by_pair.values())
    meta = {
        "database": os.environ.get("TABLEFOLD_MSSQL_DB", ""),
        "schema": schema_name,
        "dimensions": list(dims),
        "facts": list(facts),
        "declared_fk_count": len(schema.foreign_keys),
        "candidate_fk_count": len(candidates),
        "recovered_fk_count": len(recovered),
        "ddl": render_ddl(schema),
    }
    return schema.with_foreign_keys(schema.foreign_keys + recovered), meta


def _unique_subsets(cur, schema: PhysicalSchema) -> dict[str, tuple[str, ...]]:
    found: dict[str, tuple[str, ...]] = {}
    for table in schema.tables:
        if len(table.primary_key) < 2:
            continue
        for column in table.primary_key:
            if column.lower() in {"load_dt", "load_user"}:
                continue
            cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT [{column}]) FROM [{table.name}]")
            rows, distinct = cur.fetchone()
            if rows and rows == distinct:
                found[table.name] = (column,)
                break
    return found


def _violation_rate(cur, fk: ForeignKey) -> float:
    on = " AND ".join(f"t.[{b}] = s.[{a}]" for a, b in zip(fk.from_columns, fk.to_columns))
    not_null = " AND ".join(f"s.[{c}] IS NOT NULL" for c in fk.from_columns)
    cur.execute(
        f"SELECT COUNT(*) FROM [{fk.from_table}] s WHERE {not_null} "
        f"AND NOT EXISTS (SELECT 1 FROM [{fk.to_table}] t WHERE {on})"
    )
    bad = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM [{fk.from_table}] s WHERE {not_null}")
    total = cur.fetchone()[0]
    return bad / total if total else 1.0
