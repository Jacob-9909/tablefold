"""Tests for the PostgreSQL introspector's pure assembly step.

Connecting to a database is not needed to check the part most likely to be
wrong: turning four flat result sets into a schema, including composite keys
arriving as several rows that have to be regrouped in order.
"""

from __future__ import annotations

from tablefold.introspect.postgres import _assemble

TABLE_ROWS = [
    ("orders", "Customer orders", 120_000),
    ("customers", None, 4_000),
]

COLUMN_ROWS = [
    ("orders", "id", "bigint", False, None, 1),
    ("orders", "customer_id", "bigint", True, "FK to customers", 2),
    ("orders", "total", "numeric(12,2)", True, None, 3),
    ("customers", "id", "bigint", False, None, 1),
    ("customers", "email", "character varying(255)", False, None, 2),
]

PK_ROWS = [
    ("orders", "id", 0),
    ("customers", "id", 0),
]

FK_ROWS = [
    ("fk_orders_customer", "orders", "customer_id", "customers", "id", 1),
]


def test_assembles_tables_with_comments_and_row_counts():
    schema = _assemble("public", TABLE_ROWS, COLUMN_ROWS, PK_ROWS, FK_ROWS)

    orders = schema.table("orders")
    assert orders.schema == "public"
    assert orders.comment == "Customer orders"
    assert orders.row_estimate == 120_000
    assert orders.qualified_name == "public.orders"


def test_column_nullability_and_comments_survive():
    schema = _assemble("public", TABLE_ROWS, COLUMN_ROWS, PK_ROWS, FK_ROWS)
    orders = schema.table("orders")

    assert orders.column("id").nullable is False
    assert orders.column("customer_id").nullable is True
    assert orders.column("customer_id").comment == "FK to customers"


def test_postgres_type_names_classify_correctly():
    schema = _assemble("public", TABLE_ROWS, COLUMN_ROWS, PK_ROWS, FK_ROWS)

    assert schema.table("orders").column("total").is_numeric
    # `character varying(255)` has to normalise to varchar, not to `character`.
    assert schema.table("customers").column("email").is_textual


def test_foreign_keys_are_regrouped_by_constraint():
    schema = _assemble("public", TABLE_ROWS, COLUMN_ROWS, PK_ROWS, FK_ROWS)

    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert fk.name == "fk_orders_customer"
    assert fk.from_columns == ("customer_id",)
    assert fk.inferred is False


def test_composite_keys_keep_their_column_order():
    """A two-column key arrives as two rows and must not be split or reordered."""
    table_rows = [("parent", None, 10), ("child", None, 50)]
    column_rows = [
        ("parent", "tenant_id", "bigint", False, None, 1),
        ("parent", "id", "bigint", False, None, 2),
        ("child", "tenant_id", "bigint", False, None, 1),
        ("child", "parent_id", "bigint", True, None, 2),
    ]
    pk_rows = [("parent", "tenant_id", 0), ("parent", "id", 1)]
    fk_rows = [
        ("fk_child", "child", "tenant_id", "parent", "tenant_id", 1),
        ("fk_child", "child", "parent_id", "parent", "id", 2),
    ]

    schema = _assemble("public", table_rows, column_rows, pk_rows, fk_rows)

    assert schema.table("parent").primary_key == ("tenant_id", "id")
    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert fk.from_columns == ("tenant_id", "parent_id")
    assert fk.to_columns == ("tenant_id", "id")


def test_tables_with_no_readable_columns_are_dropped():
    schema = _assemble(
        "public", [("ghost", None, 0), *TABLE_ROWS], COLUMN_ROWS, PK_ROWS, FK_ROWS
    )

    assert "ghost" not in schema.table_names


def test_assembled_schema_folds():
    from tablefold.cluster import SelectionPolicy
    from tablefold.pipeline import fold

    schema = _assemble("public", TABLE_ROWS, COLUMN_ROWS, PK_ROWS, FK_ROWS)
    result = fold(schema, policy=SelectionPolicy(max_areas=1))

    orders = result.layer.model("orders")
    assert orders is not None
    assert orders.field("customer_email") is not None
