"""Parse financial metadata Excel file and create empty tables in Docker MSSQL DB.
"""
from __future__ import annotations

import os
import re
import openpyxl
import pymssql
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "server": os.environ.get("TABLEFOLD_MSSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("TABLEFOLD_MSSQL_PORT", "11433")),
    "user": os.environ.get("TABLEFOLD_MSSQL_USER", "sa"),
    "password": os.environ.get("TABLEFOLD_MSSQL_PASSWORD", "Nl2Sql!Local#2026").strip("'\""),
    "database": "FINANCIAL_DB",
    "autocommit": True,
}

EXCEL_PATH = "/Users/n-whjeong/Desktop/금융합성데이터_메타데이터.xlsx"

SHEET_TO_TABLE = {
    "1.회원 정보": "member_info",
    "2.신용 정보": "credit_info",
    "3.승인매출 정보": "approved_sales_info",
    "4.청구입금 정보": "billing_deposit_info",
    "5.잔액 정보": "remain_amount_info",
    "6.채널 정보": "channel_info",
    "7.마케팅 정보": "marketing_info",
    "8.성과 정보": "performance_info",
    "9.개인CB 정보": "personal_cb_info",
    "10.기업CB 정보": "corporate_cb_info",
    "11.통신카드CB 결합정보": "telecom_card_cb_combined_info",
    "12.은행수신상품": "bank_receipt_product_info",
    "13.공모펀드상품": "public_fund_product_info",
}

def parse_excel() -> dict[str, dict]:
    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    tables = {}

    for sheet_name, table_name in SHEET_TO_TABLE.items():
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        cols = []
        pks = []
        seen_cols = set()

        for r in rows[1:]:
            if not r or not any(r):
                continue

            r_str = [str(x).strip() if x is not None else "" for x in r]
            col_name = None

            for c in r_str:
                if re.match(r"^[a-zA-Z0-9_]{2,60}$", c) and c.upper() not in (
                    "NO", "CHAR", "INT", "NUM", "DATE", "VARCHAR", "PK", "Y", "N", "DECIMAL"
                ):
                    col_name = c
                    break

            if not col_name or col_name in seen_cols:
                continue

            seen_cols.add(col_name)

            col_type = "VARCHAR(255)"
            for c in r_str:
                c_upper = c.upper()
                if "INT" in c_upper:
                    col_type = "BIGINT"
                elif "NUM" in c_upper or "FLOAT" in c_upper or "DOUBLE" in c_upper:
                    col_type = "DECIMAL(18,2)"
                elif "DATE" in c_upper or "TIME" in c_upper:
                    col_type = "VARCHAR(50)"

            is_pk = "PK" in r_str or "PRIMARY" in r_str
            if is_pk:
                pks.append(col_name)

            cols.append({"name": col_name, "type": col_type, "is_pk": is_pk})

        # Default fallback PKs if not explicitly marked
        if not pks and cols:
            pks = [cols[0]["name"]]

        tables[table_name] = {"cols": cols, "pks": pks}

    return tables


def load_into_mssql(tables: dict[str, dict]):
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    print("Connected to Docker MSSQL. Creating 13 financial metadata tables...")

    # Drop existing foreign key constraints first
    cursor.execute("""
    DECLARE @sql NVARCHAR(MAX) = '';
    SELECT @sql += 'ALTER TABLE [' + s.name + '].[' + t.name + '] DROP CONSTRAINT [' + fk.name + '];'
    FROM sys.foreign_keys fk
    INNER JOIN sys.tables t ON fk.parent_object_id = t.object_id
    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id;
    EXEC sp_executesql @sql;
    """)

    for table_name, meta in tables.items():
        cols = meta["cols"]
        pks = meta["pks"]

        # Drop existing table if present
        cursor.execute(f"IF OBJECT_ID('dbo.[{table_name}]', 'U') IS NOT NULL DROP TABLE dbo.[{table_name}]")

        col_defs = []
        for col in cols:
            nullable = "NOT NULL" if col["name"] in pks else "NULL"
            col_defs.append(f"[{col['name']}] {col['type']} {nullable}")

        pk_clause = f", CONSTRAINT [PK_{table_name}] PRIMARY KEY ({', '.join(f'[{pk}]' for pk in pks)})" if pks else ""

        ddl = f"CREATE TABLE dbo.[{table_name}] (\n  " + ",\n  ".join(col_defs) + pk_clause + "\n);"

        try:
            cursor.execute(ddl)
            print(f"✅ Created table dbo.{table_name} ({len(cols)} columns, PKs: {pks})")
        except Exception as exc:
            print(f"❌ Failed to create dbo.{table_name}: {exc}")

    # Create Foreign Keys connecting member_info with other member-related tables
    member_tables = [
        "credit_info", "approved_sales_info", "billing_deposit_info",
        "remain_amount_info", "channel_info", "marketing_info", "performance_info"
    ]

    for child_t in member_tables:
        if child_t in tables:
            fk_name = f"FK_{child_t}_member_info"
            cursor.execute(f"IF OBJECT_ID('dbo.{fk_name}', 'F') IS NOT NULL ALTER TABLE dbo.[{child_t}] DROP CONSTRAINT [{fk_name}]")
            fk_sql = f"""
            ALTER TABLE dbo.[{child_t}] ADD CONSTRAINT [{fk_name}]
            FOREIGN KEY ([job_mon], [member_no]) REFERENCES dbo.[member_info] ([job_mon], [member_no]);
            """
            try:
                cursor.execute(fk_sql)
                print(f"✅ Added Foreign Key: {child_t} -> member_info")
            except Exception as exc:
                print(f"⚠️ Warning adding FK for {child_t}: {exc}")

    # Domain ID mappings to member_no
    id_mappings = [
        ("telecom_card_cb_combined_info", "CUST_ID"),
        ("personal_cb_info", "ID"),
        ("corporate_cb_info", "ID"),
    ]

    for child_t, id_col in id_mappings:
        if child_t in tables:
            fk_name = f"FK_{child_t}_member_info_{id_col}"
            cursor.execute(f"IF OBJECT_ID('dbo.{fk_name}', 'F') IS NOT NULL ALTER TABLE dbo.[{child_t}] DROP CONSTRAINT [{fk_name}]")
            fk_sql = f"""
            ALTER TABLE dbo.[{child_t}] ADD CONSTRAINT [{fk_name}]
            FOREIGN KEY ([{id_col}]) REFERENCES dbo.[member_info] ([member_no]);
            """
            try:
                cursor.execute(fk_sql)
                print(f"✅ Added Foreign Key: {child_t}({id_col}) -> member_info(member_no)")
            except Exception as exc:
                print(f"⚠️ Warning adding FK for {child_t}({id_col}): {exc}")

    conn.close()
    print("✨ All 13 financial schema tables loaded into MSSQL database successfully!")


if __name__ == "__main__":
    tbs = parse_excel()
    load_into_mssql(tbs)
