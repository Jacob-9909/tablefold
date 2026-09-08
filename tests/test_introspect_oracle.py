"""Oracle 카탈로그 행 → :class:`PhysicalSchema` 조립.

라이브 DB 없이 돈다. 드라이버가 돌려주는 행 모양을 그대로 흉내 내고, 조립
로직만 검사한다 — 접속은 :class:`OracleIntrospector` 의 몫이고 여기서 볼 것이
아니다.
"""

from __future__ import annotations

import pytest

from tablefold.read.oracle import DIALECT, assemble, render_type


def test_dialect_is_what_sqlglot_knows():
    import sqlglot

    sqlglot.parse_one("SELECT 1", read=DIALECT)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("VARCHAR2", 50, None, None), "VARCHAR2(50)"),
        (("NVARCHAR2", 20, None, None), "NVARCHAR2(20)"),
        (("CHAR", 2, None, None), "CHAR(2)"),
        (("NUMBER", 22, 10, 2), "NUMBER(10,2)"),
        (("NUMBER", 22, 10, 0), "NUMBER(10)"),
        (("NUMBER", 22, 10, None), "NUMBER(10)"),
        # 정밀도가 없는 NUMBER 는 이름만 남아야 한다 — NUMBER(None) 은 안 된다.
        (("NUMBER", 22, None, None), "NUMBER"),
        (("DATE", 7, None, None), "DATE"),
        (("CLOB", 4000, None, None), "CLOB"),
    ],
)
def test_render_type_rebuilds_the_declaration(args, expected):
    assert render_type(*args) == expected


@pytest.fixture
def rows():
    table_rows = [
        ("ORDERS", "주문", 1200),
        ("CUSTOMERS", None, None),  # 통계 미수집 → num_rows 가 NULL
        ("EMPTY_TAB", None, 0),  # 컬럼이 없으면 조립에서 빠진다
    ]
    column_rows = [
        ("ORDERS", "ID", "NUMBER", 22, 10, 0, 0, "주문번호", 1),
        ("ORDERS", "CUSTOMER_ID", "NUMBER", 22, 10, 0, 1, None, 2),
        ("ORDERS", "TOTAL", "NUMBER", 22, 12, 2, 1, None, 3),
        ("CUSTOMERS", "ID", "NUMBER", 22, 10, 0, 0, None, 1),
        ("CUSTOMERS", "EMAIL", "VARCHAR2", 320, None, None, 1, None, 2),
    ]
    pk_rows = [("ORDERS", "ID", 1), ("CUSTOMERS", "ID", 1)]
    fk_rows = [("FK_ORDERS_CUSTOMER", "ORDERS", "CUSTOMER_ID", "CUSTOMERS", "ID", 1)]
    return table_rows, column_rows, pk_rows, fk_rows


def test_assemble_builds_tables_columns_and_keys(rows):
    schema = assemble("SALES", *rows)

    assert {t.name for t in schema.tables} == {"ORDERS", "CUSTOMERS"}
    orders = schema.table("ORDERS")
    assert orders.schema == "SALES"
    assert orders.comment == "주문"
    assert orders.row_estimate == 1200
    assert orders.primary_key == ("ID",)
    assert orders.column_names == ("ID", "CUSTOMER_ID", "TOTAL")
    assert orders.columns[0].nullable is False
    assert orders.columns[1].nullable is True
    assert orders.columns[2].type.upper().startswith("NUMBER(12,2)")


def test_assemble_drops_a_table_with_no_columns(rows):
    schema = assemble("SALES", *rows)
    assert schema.table("EMPTY_TAB") is None


def test_assemble_keeps_a_missing_row_estimate_as_none(rows):
    """통계를 안 돌린 테이블은 0 이 아니라 '모른다' 여야 한다.

    0 으로 채우면 :mod:`tablefold.choose.classify` 가 빈 테이블로 보고 크기
    가중치를 깎는다 — 실제로는 클 수도 있는 표가 앵커 경쟁에서 밀린다.
    """
    schema = assemble("SALES", *rows)
    assert schema.table("CUSTOMERS").row_estimate is None


def test_assemble_groups_a_composite_foreign_key():
    """복합키는 제약 이름 하나로 묶이고 position 순서를 지켜야 한다."""
    fk_rows = [
        ("FK_TWO", "CHILD", "A_ID", "PARENT", "PA", 1),
        ("FK_TWO", "CHILD", "B_ID", "PARENT", "PB", 2),
    ]
    column_rows = [
        ("CHILD", "A_ID", "NUMBER", 22, 10, 0, 0, None, 1),
        ("CHILD", "B_ID", "NUMBER", 22, 10, 0, 0, None, 2),
        ("PARENT", "PA", "NUMBER", 22, 10, 0, 0, None, 1),
        ("PARENT", "PB", "NUMBER", 22, 10, 0, 0, None, 2),
    ]
    schema = assemble(
        "SALES", [("CHILD", None, 1), ("PARENT", None, 1)], column_rows, [], fk_rows
    )

    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert fk.from_columns == ("A_ID", "B_ID")
    assert fk.to_columns == ("PA", "PB")


def test_assembled_schema_folds(rows):
    """조립 결과가 실제로 파이프라인에 들어간다 — 계약이 맞는지 끝까지 확인."""
    from tablefold.fold import fold

    result = fold(assemble("SALES", *rows))
    assert result.layer.models
