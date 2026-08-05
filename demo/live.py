"""라이브 데이터베이스를 데모의 스키마 소스로 붙인다.

접속 정보는 환경 변수로만 받는다. 데모라도 자격 증명을 소스에 두지 않는다.

접속이 안 되면 **가짜 스키마로 대신하지 않는다.** 한때 그렇게 했었고, 화면은
"데이터베이스 연결"이라고 말하면서 지어낸 6개 테이블을 보여 줬다. 읽는 사람은
자기 데이터베이스를 보고 있다고 믿는데 실제로는 아무 관계도 없는 숫자였다.
연결이 없으면 없다고 말하고 예제 DDL 쪽으로 보내는 편이 낫다 — 그쪽은 적어도
예제라고 이름이 붙어 있다.
"""

from __future__ import annotations

import functools
import os

from tablefold.graph.from_keys import infer_from_primary_keys
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
    """실제로 붙을 수 있을 때만 참.

    환경 변수만 보고 참을 돌려주면, 값이 틀렸거나 데이터베이스가 꺼져 있을 때
    화면이 "연결됨"이라고 말한 뒤 조회 단계에서야 터진다. 여기서 한 번 붙어
    본다 — 화면이 켜질 때 한 번이라 비용은 무시할 만하다.
    """
    if not available_real_db():
        return False
    try:
        connect = _connect()
        conn = connect()
    except Exception:  # noqa: BLE001 — 접속 실패의 종류는 여기서 중요하지 않다
        return False
    conn.close()
    return True


def render_ddl(schema: PhysicalSchema) -> str:
    blocks = []
    for table in schema.tables:
        lines = [f"  {c.name} {c.type}" for c in table.columns]
        if table.primary_key:
            lines.append(f"  PRIMARY KEY ({', '.join(table.primary_key)})")
        blocks.append(f"CREATE TABLE {table.name} (\n" + ",\n".join(lines) + "\n);")
    return "\n\n".join(blocks)


def load(schema_name: str = "dbo") -> tuple[PhysicalSchema, dict]:
    """스키마를 읽고, 데이터로 검증한 외래 키를 붙여 돌려준다."""
    if not available_real_db():
        raise LiveUnavailable(
            "데이터베이스 접속 정보가 없습니다. "
            "TABLEFOLD_MSSQL_HOST / PORT / USER / PASSWORD / DB 를 설정하세요."
        )
    return _load_real(schema_name)


def available_real_db() -> bool:
    return bool(
        os.environ.get("TABLEFOLD_MSSQL_HOST")
        and os.environ.get("TABLEFOLD_MSSQL_DB")
    )


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
