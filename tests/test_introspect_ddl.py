from __future__ import annotations

from tablefold.read.ddl import DDLIntrospector

INLINE_DDL = """
CREATE TABLE teams (
    id   bigint PRIMARY KEY,
    name varchar(100) NOT NULL
);

CREATE TABLE players (
    id      bigint PRIMARY KEY,
    team_id bigint REFERENCES teams (id),
    goals   integer
);
"""

ALTER_DDL = """
CREATE TABLE a (id bigint PRIMARY KEY, b_id bigint);
CREATE TABLE b (id bigint PRIMARY KEY);
ALTER TABLE a ADD CONSTRAINT fk_a_b FOREIGN KEY (b_id) REFERENCES b (id);
"""

COMPOSITE_DDL = """
CREATE TABLE parent (
    tenant_id bigint,
    id        bigint,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE child (
    id            bigint PRIMARY KEY,
    tenant_id     bigint,
    parent_id     bigint,
    CONSTRAINT fk_child_parent FOREIGN KEY (tenant_id, parent_id)
        REFERENCES parent (tenant_id, id)
);
"""


def test_reads_columns_and_primary_keys():
    schema = DDLIntrospector(INLINE_DDL).introspect()

    players = schema.table("players")
    assert players is not None
    assert players.primary_key == ("id",)
    assert players.column_names == ("id", "team_id", "goals")
    assert players.column("goals").is_numeric


def test_reads_inline_references():
    schema = DDLIntrospector(INLINE_DDL).introspect()

    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert (fk.from_table, fk.from_columns) == ("players", ("team_id",))
    assert (fk.to_table, fk.to_columns) == ("teams", ("id",))
    assert fk.inferred is False


def test_reads_alter_table_foreign_keys():
    schema = DDLIntrospector(ALTER_DDL).introspect()

    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert fk.from_table == "a"
    assert fk.to_table == "b"


def test_reads_composite_keys():
    schema = DDLIntrospector(COMPOSITE_DDL).introspect()

    assert schema.table("parent").primary_key == ("tenant_id", "id")
    fk = schema.foreign_keys[0]
    assert fk.from_columns == ("tenant_id", "parent_id")
    assert fk.to_columns == ("tenant_id", "id")


def test_drops_foreign_keys_pointing_outside_the_schema():
    ddl = """
    CREATE TABLE a (id bigint PRIMARY KEY, z_id bigint);
    ALTER TABLE a ADD CONSTRAINT fk_a_z FOREIGN KEY (z_id) REFERENCES elsewhere (id);
    """
    schema = DDLIntrospector(ddl).introspect()

    assert schema.table_names == ("a",)
    assert schema.foreign_keys == ()


def test_reads_the_full_retail_fixture(retail_schema):
    assert len(retail_schema.tables) == 53
    assert len(retail_schema.foreign_keys) == 68
    assert all(t.primary_key for t in retail_schema.tables)

    orders = retail_schema.table("orders")
    assert orders.column("grand_total").is_numeric
    assert orders.column("placed_at").is_temporal
    assert orders.column("order_number").is_textual
