"""라이브 데이터베이스를 데모의 스키마 소스로 붙인다.

접속 정보는 환경 변수로만 받는다. 데모라도 자격 증명을 소스에 두지 않는다.

접속이 안 되면 **가짜 스키마로 대신하지 않는다.** 한때 그렇게 했었고, 화면은
"데이터베이스 연결"이라고 말하면서 지어낸 6개 테이블을 보여 줬다. 읽는 사람은
자기 데이터베이스를 보고 있다고 믿는데 실제로는 아무 관계도 없는 숫자였다.
연결이 없으면 없다고 말하고 예제 DDL 쪽으로 보내는 편이 낫다 — 그쪽은 적어도
예제라고 이름이 붙어 있다.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from tablefold.ir import PhysicalSchema
from tablefold.read.mssql import (
    MSSQLIntrospector,
    MSSQLUnavailable,
    connect_from_env,
    env_configured,
)
from tablefold.relate.validate import DEFAULT_VIOLATION_TOLERANCE, recover_with_data

load_dotenv()

VIOLATION_TOLERANCE = DEFAULT_VIOLATION_TOLERANCE


class LiveUnavailable(RuntimeError):
    """라이브 소스가 설정되지 않았거나 접속할 수 없다."""


def _connect():
    """접속 팩토리. 로직은 :mod:`tablefold.read.mssql` 에 있다 — 화면과 CLI 가
    같은 자격 증명과 같은 실패 메시지를 쓰게 하려는 것이다."""
    try:
        return connect_from_env()
    except MSSQLUnavailable as exc:
        raise LiveUnavailable(str(exc)) from exc


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


def _connect_db(database_name: str):
    """접속 팩토리. 자격 증명은 환경 변수로만 받는다.

    기본값으로 ``sa`` 와 비밀번호를 심어 둔 적이 있다. 편의였지만, 이 SQL 을
    실행하는 창구가 인터넷에 열려 있는 순간 그 기본값은 데이터베이스 전체
    권한을 익명에게 넘기는 일이었다. 값이 없으면 접속을 거절한다 — 데모가
    죽는 것보다 남의 웨어하우스가 열리는 것이 나쁘다.
    """
    user = os.environ.get("TABLEFOLD_MSSQL_USER")
    password = os.environ.get("TABLEFOLD_MSSQL_PASSWORD")
    if not user or not password:
        raise LiveUnavailable(
            "TABLEFOLD_MSSQL_USER / TABLEFOLD_MSSQL_PASSWORD 가 설정되지 않아 "
            "라이브 소스에 붙을 수 없습니다. 예제 스키마 모드를 사용하세요."
        )

    def connect_fn():
        import pymssql
        return pymssql.connect(
            server=os.environ.get("TABLEFOLD_MSSQL_HOST", "localhost"),
            port=int(os.environ.get("TABLEFOLD_MSSQL_PORT", "11433")),
            user=user,
            password=password.strip("'\""),
            database=database_name,
        )
    return connect_fn



def load_financial(schema_name: str = "dbo") -> tuple[PhysicalSchema, dict]:
    """독립된 FINANCIAL_DB 데이터베이스에서 13개 금융합성 스키마를 읽어 돌려준다."""
    if not available_real_db():
        raise LiveUnavailable("데이터베이스 접속 정보가 없습니다.")

    connect = _connect_db("FINANCIAL_DB")
    raw_schema = MSSQLIntrospector(connect, schema=schema_name).introspect()

    from tablefold.ir import ForeignKey
    inferred_fks = [
        ForeignKey(from_table="telecom_card_cb_combined_info", from_columns=("CUST_ID",), to_table="member_info", to_columns=("member_no",), inferred=True),
        ForeignKey(from_table="personal_cb_info", from_columns=("ID",), to_table="member_info", to_columns=("member_no",), inferred=True),
        ForeignKey(from_table="corporate_cb_info", from_columns=("ID",), to_table="member_info", to_columns=("member_no",), inferred=True),
    ]

    schema = PhysicalSchema(
        tables=raw_schema.tables,
        foreign_keys=raw_schema.foreign_keys + tuple(inferred_fks)
    )

    dims = tuple(t.name for t in schema.tables if t.name == "member_info")
    facts = tuple(t.name for t in schema.tables if t.name != "member_info")

    conn = connect()
    try:
        schema, recovered = recover_with_data(
            schema, conn.cursor(), targets=dims or None
        )
    finally:
        conn.close()

    meta = {
        "database": "FINANCIAL_DB",
        "schema": schema_name,
        "dimensions": list(dims),
        "facts": list(facts),
        "declared_fk_count": len([fk for fk in schema.foreign_keys if not fk.inferred]),
        "candidate_fk_count": len(recovered),
        "recovered_fk_count": len(recovered),
        "ddl": render_ddl(schema),
    }
    return schema, meta




def available_real_db() -> bool:
    return env_configured()


def _load_real(schema_name: str = "dbo") -> tuple[PhysicalSchema, dict]:
    connect = _connect()
    try:
        schema = MSSQLIntrospector(connect, schema=schema_name).introspect()
    except Exception as exc:
        raise LiveUnavailable(f"데이터베이스에 접속하지 못했습니다: {exc}") from exc

    dims = tuple(t.name for t in schema.tables if t.name.upper().startswith("D_"))
    facts = tuple(t.name for t in schema.tables if t.name.upper().startswith("F_"))

    # 관계 복구와 데이터 검증은 라이브러리에 있다. 한때 이 파일에만 있었고,
    # 그래서 CLI 로는 같은 레이어를 낼 수 없었다 — 같은 스키마인데 화면과
    # 명령줄이 다른 답을 주는 상태였다.
    conn = connect()
    try:
        schema, recovered = recover_with_data(
            schema, conn.cursor(), targets=dims or None
        )
    finally:
        conn.close()

    meta = {
        "database": os.environ.get("TABLEFOLD_MSSQL_DB", ""),
        "schema": schema_name,
        "dimensions": list(dims),
        "facts": list(facts),
        "declared_fk_count": len(
            [fk for fk in schema.foreign_keys if not fk.inferred]
        ),
        "candidate_fk_count": len(recovered),
        "recovered_fk_count": len(recovered),
        "ddl": render_ddl(schema),
    }
    return schema, meta


def execute_query(sql: str) -> dict[str, Any]:
    """실제 데이터베이스에 쿼리를 실행하여 결과 행과 컬럼을 돌려준다."""
    if not available_real_db():
        return {
            "status": "not_connected",
            "message": "데이터베이스 연결 정보가 없습니다. (.env 파일의 TABLEFOLD_MSSQL_* 정보를 채워주세요)"
        }
    try:
        connect_fn = _connect()
        conn = connect_fn()
        cursor = conn.cursor()
        cursor.execute(sql)

        columns = [col[0] for col in cursor.description] if cursor.description else []
        raw_rows = cursor.fetchmany(50)
        conn.close()

        clean_rows = []
        for r in raw_rows:
            row_dict = {}
            for col_idx, val in enumerate(r):
                col_name = columns[col_idx] if col_idx < len(columns) else f"col_{col_idx}"
                if hasattr(val, "isoformat"):
                    row_dict[col_name] = val.isoformat()
                elif isinstance(val, (int, float, str, bool)) or val is None:
                    row_dict[col_name] = val
                else:
                    row_dict[col_name] = str(val)
            clean_rows.append(row_dict)

        return {
            "status": "success",
            "columns": columns,
            "rows": clean_rows,
            "row_count": len(clean_rows),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }

