from __future__ import annotations

from pathlib import Path

import pytest

from tablefold.graph import SchemaGraph
from tablefold.introspect.ddl import DDLIntrospector
from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def column(
    name: str, type_: str = "bigint", *, nullable: bool = True
) -> PhysicalColumn:
    return PhysicalColumn(name=name, type=type_, nullable=nullable)


@pytest.fixture
def tiny_schema() -> PhysicalSchema:
    """Four tables covering every structure the fold has to handle.

    ``orders`` is the fact. ``customers`` is a one-hop dimension and ``tiers``
    a two-hop one, so join chaining is exercised. ``order_items`` is a child, so
    the one-to-many path is exercised too.
    """
    orders = PhysicalTable(
        name="orders",
        columns=(
            column("id", nullable=False),
            column("customer_id"),
            column("total", "numeric(12,2)"),
            column("tax", "numeric(12,2)"),
            column("placed_at", "timestamp"),
        ),
        primary_key=("id",),
    )
    customers = PhysicalTable(
        name="customers",
        columns=(
            column("id", nullable=False),
            column("tier_id"),
            column("email", "varchar(255)"),
            column("name", "varchar(100)"),
        ),
        primary_key=("id",),
    )
    tiers = PhysicalTable(
        name="tiers",
        columns=(
            column("id", nullable=False),
            column("label", "varchar(50)"),
        ),
        primary_key=("id",),
    )
    order_items = PhysicalTable(
        name="order_items",
        columns=(
            column("id", nullable=False),
            column("order_id"),
            column("quantity", "integer"),
            column("line_total", "numeric(12,2)"),
        ),
        primary_key=("id",),
    )

    return PhysicalSchema(
        tables=(orders, customers, tiers, order_items),
        foreign_keys=(
            ForeignKey("orders", ("customer_id",), "customers", ("id",)),
            ForeignKey("customers", ("tier_id",), "tiers", ("id",)),
            ForeignKey("order_items", ("order_id",), "orders", ("id",)),
        ),
    )


@pytest.fixture
def tiny_graph(tiny_schema: PhysicalSchema) -> SchemaGraph:
    return SchemaGraph.build(tiny_schema)


@pytest.fixture(scope="session")
def retail_schema() -> PhysicalSchema:
    return DDLIntrospector.from_path(FIXTURES / "retail_50.sql").introspect()


@pytest.fixture(scope="session")
def retail_graph(retail_schema: PhysicalSchema) -> SchemaGraph:
    return SchemaGraph.build(retail_schema)
