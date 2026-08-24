"""이름이 말해 주지 않는 관계 발견 — 토큰 정합과 값 포함 탐사.

여기의 모든 기대값은 "관계가 있으면 찾고, 없으면 버린다"다. 놓치는 쪽은 그래프가
조각나서 답을 잃고, 지어내는 쪽은 조인이 조용히 행을 복제한다. 둘 다 조용하므로
테스트가 유일한 소음이다.
"""

from __future__ import annotations

import sqlite3

import pytest

from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable
from tablefold.read.ddl import DDLIntrospector
from tablefold.relate.discover import (
    Candidate,
    containment_rate,
    coverage,
    infer_from_name_tokens,
    probe_relationships,
    tokens,
    value_candidates,
)
from tablefold.relate.graph import SchemaGraph
from tablefold.relate.validate import recover_with_data
from tablefold.t2sql.preset import recover_relationships, split_anchors


def column(name: str, type_: str = "bigint", *, nullable: bool = True):
    return PhysicalColumn(name=name, type=type_, nullable=nullable)


# ── 어휘소 ────────────────────────────────────────────────────────────────────


def test_tokens_split_and_drop_noise():
    assert tokens("SA_ORG_CD") == ("sa", "org", "cd")
    assert tokens("F_SALES_2024") == ("f", "sales")  # 숫자 꼬리는 어휘가 아니다


def test_coverage_rewards_full_overlap():
    vocab = frozenset(tokens("D_SA_ORG") + tokens("ORG_CD"))
    assert coverage("org", vocab) == 1.0
    assert coverage("sa_org", vocab) == 1.0
    # 약어 완화: 짧은 쪽이 긴 쪽의 접두사면 같은 낱말이다.
    assert coverage("organ", frozenset({"organization"})) == 1.0
    assert coverage("fi_org", vocab) == 0.5
    assert coverage("", vocab) == 0.0


# ── 토큰 정합 (스키마만) ──────────────────────────────────────────────────────


def _schema_with_qualifier() -> PhysicalSchema:
    d_sa = PhysicalTable(
        name="D_SA_ORG",
        columns=(column("ORG_CD", "varchar"), column("HEAD_NM", "varchar")),
        primary_key=("ORG_CD",),
    )
    d_fi = PhysicalTable(
        name="D_FI_ORG",
        columns=(column("FI_ORG_CD", "varchar"), column("ACCT_NM", "varchar")),
        primary_key=("FI_ORG_CD",),
    )
    fact = PhysicalTable(
        name="F_SALES",
        columns=(
            column("YYYYMM", "varchar"),
            column("SA_ORG_CD", "varchar"),
            column("SALES_AMT", "numeric"),
        ),
    )
    return PhysicalSchema(tables=(d_sa, d_fi, fact))


def test_token_matching_finds_the_qualified_reference():
    """``SA_ORG_CD`` 의 어간 {sa, org}는 D_SA_ORG 가 전부 설명한다."""
    schema = _schema_with_qualifier()

    found = infer_from_name_tokens(schema)

    assert {(fk.from_table, fk.to_table) for fk in found} == {("F_SALES", "D_SA_ORG")}


def test_token_matching_does_not_claim_cross_name_keys():
    """CUST_ID → MEMBER_NO 는 이름 근거가 없다. 그건 값 탐사의 몫이다."""
    member = PhysicalTable(
        name="member_info",
        columns=(column("MEMBER_NO"), column("MEMBER_NM", "varchar")),
        primary_key=("MEMBER_NO",),
    )
    telecom = PhysicalTable(
        name="telecom_card_cb_combined_info",
        columns=(column("CUST_ID"),),
    )
    schema = PhysicalSchema(tables=(member, telecom))

    assert infer_from_name_tokens(schema) == ()


def test_token_matching_rejects_type_mismatch():
    org = PhysicalTable(
        name="D_ORG",
        columns=(column("ORG_CD", "varchar"),),
        primary_key=("ORG_CD",),
    )
    fact = PhysicalTable(
        name="F_SALES",
        columns=(column("SA_ORG_CD"),),  # 숫자 vs 문자 — 비교 불가능한 쌍
    )

    assert infer_from_name_tokens(PhysicalSchema(tables=(org, fact))) == ()


# ── 후보 생성 ────────────────────────────────────────────────────────────────


def test_value_candidates_rank_by_name_likeness():
    schema = _schema_with_qualifier()

    candidates = value_candidates(schema)

    pairs = [(c.from_column, c.to_table) for c in candidates]
    assert ("SA_ORG_CD", "D_SA_ORG") in pairs
    assert ("SA_ORG_CD", "D_FI_ORG") in pairs
    # 유사도 내림차순 — 더 그럴듯한 쪽이 먼저 프로브된다.
    scores = {c.to_table: c.score for c in candidates if c.from_column == "SA_ORG_CD"}
    assert scores["D_SA_ORG"] > scores["D_FI_ORG"]


def test_declared_edges_are_not_candidates_again():
    org = PhysicalTable(
        name="D_ORG",
        columns=(column("ORG_CD", "varchar"),),
        primary_key=("ORG_CD",),
    )
    fact = PhysicalTable(
        name="F_SALES",
        columns=(column("ORG_CD", "varchar"),),
    )
    declared = PhysicalSchema(
        tables=(org, fact),
        foreign_keys=(ForeignKey("F_SALES", ("ORG_CD",), "D_ORG", ("ORG_CD",)),),
    )

    assert value_candidates(declared) == ()


def test_polymorphic_columns_are_never_probed():
    """모든 표의 id 집합은 서로 겹치기 쉽다. 값 포함이 우연히 통과한다."""
    a = PhysicalTable(name="A", columns=(column("id"),))
    b = PhysicalTable(name="B", columns=(column("id"),))

    assert value_candidates(PhysicalSchema(tables=(a, b))) == ()


# ── 값 포함 (sqlite 실측) ─────────────────────────────────────────────────────


class CountingCursor:
    """실행된 질의 수를 센다. 예산 계약의 증인."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.count = 0

    def execute(self, operation: str, /) -> None:
        self.count += 1
        self.inner.execute(operation)

    def fetchone(self):
        return self.inner.fetchone()


@pytest.fixture
def member_db():
    """live.py 가 하드코딩으로 때운 바로 그 모양. 자동 발견되어야 한다."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE member_info (
            MEMBER_NO INTEGER PRIMARY KEY,
            GRADE VARCHAR(10)
        );
        CREATE TABLE telecom_card_cb_combined_info (
            SEQ INTEGER PRIMARY KEY,
            CUST_ID INTEGER
        );
        CREATE TABLE decoy_dim (
            ZOMBIE_NO INTEGER PRIMARY KEY
        );
        INSERT INTO member_info VALUES (1,'A'),(2,'B'),(3,'C'),(4,'D');
        INSERT INTO telecom_card_cb_combined_info VALUES
            (101,1),(102,3),(103,4),(104,NULL);
        INSERT INTO decoy_dim VALUES (900),(901);
        """
    )
    yield conn
    conn.close()


def financial_schema() -> PhysicalSchema:
    member = PhysicalTable(
        name="member_info",
        columns=(column("MEMBER_NO"), column("GRADE", "varchar")),
        primary_key=("MEMBER_NO",),
    )
    telecom = PhysicalTable(
        name="telecom_card_cb_combined_info",
        columns=(column("SEQ"), column("CUST_ID")),
        primary_key=("SEQ",),
    )
    decoy = PhysicalTable(
        name="decoy_dim",
        columns=(column("ZOMBIE_NO"),),
        primary_key=("ZOMBIE_NO",),
    )
    return PhysicalSchema(tables=(member, telecom, decoy))


def test_containment_counts_distinct_values_not_rows(member_db):
    cur = member_db.cursor()
    candidate = Candidate(
        from_table="telecom_card_cb_combined_info",
        from_column="CUST_ID",
        to_table="member_info",
        to_column="MEMBER_NO",
        score=0.0,
    )

    rate, total = containment_rate(candidate, cur, dialect="tsql")

    # 구별되는 값 {1,3,4} 세 개가 전부 부모에 들어간다. NULL 은 세지 않는다.
    assert total == 3
    assert rate == 0.0


def test_probe_discovers_cross_name_references(member_db):
    """이름 규칙 두 개가 다 놓친 관계를 값이 찾는다."""
    schema = financial_schema()
    cur = member_db.cursor()

    found = probe_relationships(schema, cur, dialect="tsql")

    assert {(fk.from_table, fk.to_table) for fk in found} == {
        ("telecom_card_cb_combined_info", "member_info"),
    }
    fk = next(iter(found))
    assert fk.confidence >= 0.99
    assert fk.inferred is True


def test_probe_budget_limits_queries(member_db):
    """예산이 질의 수를 묶는다. 잘리면 발견도 같이 줄어든다 — 정직한 절충."""
    schema = financial_schema()
    counting = CountingCursor(member_db.cursor())

    found = probe_relationships(schema, counting, dialect="tsql", max_probes=1)

    assert counting.count == 1
    # 동점(유사도 0) 후보는 이름순이라 decoy 가 먼저다. 값이 안 맞아 탈락하고
    # 예산이 끝났으므로 진짜 관계도 이번엔 못 찾는다.
    assert found == ()


def test_probe_keeps_the_best_parent_when_two_pass():
    """한 컬럼이 두 부모를 통과하면 위반율이 낮은 쪽 하나만 남긴다."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE wide_dim (CODE INTEGER PRIMARY KEY);
        CREATE TABLE narrow_dim (CODE INTEGER PRIMARY KEY);
        CREATE TABLE child (SEQ INTEGER PRIMARY KEY, CODE INTEGER);
        INSERT INTO wide_dim VALUES (1),(2),(3),(9);
        INSERT INTO narrow_dim VALUES (1),(2),(3);
        INSERT INTO child VALUES (1,1),(2,2),(3,3);
        """
    )
    wide = PhysicalTable(
        name="wide_dim", columns=(column("CODE"),), primary_key=("CODE",)
    )
    narrow = PhysicalTable(
        name="narrow_dim", columns=(column("CODE"),), primary_key=("CODE",)
    )
    child = PhysicalTable(
        name="child", columns=(column("SEQ"), column("CODE")), primary_key=("SEQ",)
    )
    schema = PhysicalSchema(tables=(wide, narrow, child))

    found = probe_relationships(schema, conn.cursor(), dialect="tsql")

    assert [(fk.to_table, fk.confidence) for fk in found] == [("narrow_dim", 1.0)]
    conn.close()


# ── 형제 가드 ────────────────────────────────────────────────────────────────


def test_a_shared_code_domain_is_not_a_relationship():
    """팩트둘이 같은 코드 영역을 들고 있으면 위반율까지 통과한다 — 사촌이다.

    ``F_A.ORG_CD → F_B.ORG_CD`` 는 데이터로도 완벽히 들어맞는다(O1,O2 ⊆ O2,O3).
    이걸 받으면 사촌 팩트 사이의 조인이 생기고 행이 복제된다. 형제 가드가
    없었다면 이 엣지는 살아 있었을 것이다 — 부재를 상상해 보라.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE D_ORG (ORG_CD VARCHAR PRIMARY KEY);
        CREATE TABLE F_A (A_ID INTEGER PRIMARY KEY, ORG_CD VARCHAR);
        CREATE TABLE F_B (ORG_CD VARCHAR PRIMARY KEY);
        INSERT INTO D_ORG VALUES ('O1'),('O2'),('O3');
        INSERT INTO F_A VALUES (1,'O2'),(2,'O2');
        INSERT INTO F_B VALUES ('O2'),('O3');
        """
    )
    d_org = PhysicalTable(
        name="D_ORG", columns=(column("ORG_CD", "varchar"),), primary_key=("ORG_CD",)
    )
    f_a = PhysicalTable(
        name="F_A",
        columns=(column("A_ID"), column("ORG_CD", "varchar")),
        primary_key=("A_ID",),
    )
    f_b = PhysicalTable(
        name="F_B", columns=(column("ORG_CD", "varchar"),), primary_key=("ORG_CD",)
    )
    schema = PhysicalSchema(
        tables=(d_org, f_a, f_b),
        foreign_keys=(
            ForeignKey("F_A", ("ORG_CD",), "D_ORG", ("ORG_CD",)),
            ForeignKey("F_B", ("ORG_CD",), "D_ORG", ("ORG_CD",)),
        ),
    )

    _, recovered = recover_with_data(schema, conn.cursor(), dialect="tsql")

    pairs = {(fk.from_table, fk.to_table) for fk in recovered}
    assert ("F_A", "F_B") not in pairs
    assert ("F_B", "F_A") not in pairs
    conn.close()


# ── 스노우플레이크 체인 ───────────────────────────────────────────────────────


def test_recovery_bridges_the_snowflake_middle():
    """F → D_MID → D_TOP 에서 중간 차원까지 닿아야 한다.

    한때 참조 대상을 나가는 키가 없는 표로 제한해서 D_MID 를 못 잡았고, 팩트의
    입도에서 중간 차원을 묻는 질문이 전부 죽었다.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE D_TOP (TOP_CD VARCHAR PRIMARY KEY, TOP_NM VARCHAR);
        CREATE TABLE D_MID (MID_CD VARCHAR PRIMARY KEY, TOP_CD VARCHAR);
        CREATE TABLE F_SALES (
            SALES_ID INTEGER PRIMARY KEY, MID_CD VARCHAR, AMT NUMERIC
        );
        INSERT INTO D_TOP VALUES ('T1','본부'),('T2','지사');
        INSERT INTO D_MID VALUES ('M1','T1'),('M2','T2');
        INSERT INTO F_SALES VALUES (1,'M1',100),(2,'M2',200);
        """
    )
    d_top = PhysicalTable(
        name="D_TOP",
        columns=(column("TOP_CD", "varchar"), column("TOP_NM", "varchar")),
        primary_key=("TOP_CD",),
    )
    d_mid = PhysicalTable(
        name="D_MID",
        columns=(column("MID_CD", "varchar"), column("TOP_CD", "varchar")),
        primary_key=("MID_CD",),
    )
    f_sales = PhysicalTable(
        name="F_SALES",
        columns=(
            column("SALES_ID"),
            column("MID_CD", "varchar"),
            column("AMT", "numeric"),
        ),
        primary_key=("SALES_ID",),
    )
    declared = ForeignKey("D_MID", ("TOP_CD",), "D_TOP", ("TOP_CD",))
    schema = PhysicalSchema(tables=(d_top, d_mid, f_sales), foreign_keys=(declared,))

    enriched, recovered = recover_with_data(schema, conn.cursor(), dialect="tsql")

    graph = SchemaGraph.build(enriched)
    assert graph.out_degree("F_SALES") == 1  # F_SALES → D_MID 가 생겼다
    assert {fk.to_table for fk in recovered} == {"D_MID"}
    conn.close()


def test_explicit_targets_stay_respected_but_probes_still_run(member_db):
    """호출자가 대상을 명시하면 확장하지 않는다. 프로브는 별개로 작동한다.

    ``demo/live.py`` 의 금융 스키마 적재가 바로 이 모양이다 — 대상을
    ``member_info`` 로 좍 좁혀 주지만, ``CUST_ID → MEMBER_NO`` 는 이름이 달라서
    여전히 프로브의 몫이다.
    """
    schema = financial_schema()

    enriched, recovered = recover_with_data(
        schema, member_db.cursor(), targets=("member_info",), dialect="tsql"
    )

    assert {(fk.from_table, fk.to_table) for fk in recovered} == {
        ("telecom_card_cb_combined_info", "member_info"),
    }


# ── 스타 프리셋 편입 ─────────────────────────────────────────────────────────


def test_an_edgeless_table_with_a_key_becomes_a_dimension():
    """간선 없는 표는 한때 조용히 버려졌다. 이제 앵커로 산다."""
    rates = PhysicalTable(
        name="exchange_rates",
        columns=(column("code", "varchar"), column("rate", "numeric")),
        primary_key=("code",),
    )
    orders = PhysicalTable(
        name="orders",
        columns=(column("id"), column("customer_id")),
        primary_key=("id",),
    )
    customers = PhysicalTable(
        name="customers", columns=(column("id"),), primary_key=("id",)
    )
    schema = PhysicalSchema(
        tables=(rates, orders, customers),
        foreign_keys=(ForeignKey("orders", ("customer_id",), "customers", ("id",)),),
    )
    graph = SchemaGraph.build(schema)

    dimensions, facts = split_anchors(graph)

    assert set(dimensions) == {"exchange_rates", "customers"}
    assert set(facts) == {"orders"}


def test_an_edgeless_table_without_a_key_stays_out():
    """기본 키조차 없는 표는 앵커로 세울 근거가 없다."""
    logs = PhysicalTable(name="raw_events", columns=(column("payload", "text"),))
    graph = SchemaGraph.build(PhysicalSchema(tables=(logs,)))

    dimensions, facts = split_anchors(graph)

    assert dimensions == ()
    assert facts == ()


def test_schema_only_recovery_uses_tokens_for_qualified_names():
    """데이터 없이도 SA_ORG_CD → D_SA_ORG 는 복구된다."""
    schema = _schema_with_qualifier()

    recovered = recover_relationships(schema)

    added = set(recovered.foreign_keys) - set(schema.foreign_keys)
    assert {(fk.from_table, fk.to_table) for fk in added} == {("F_SALES", "D_SA_ORG")}


def test_schema_only_recovery_does_not_bridge_to_middles():
    """스키마만 보는 경로는 중간 차원 확장을 하지 않는다.

    데이터 없이는 중간 차원과 팩트를 가릴 수 없다. 가릴 수 없는 선택은 하지
    않는 것이 맞다 — 데이터 경로가 한다.
    """
    ddl = """
    CREATE TABLE D_TOP (TOP_CD VARCHAR(10) PRIMARY KEY);
    CREATE TABLE D_MID (
        MID_CD VARCHAR(10) PRIMARY KEY,
        TOP_CD VARCHAR(10),
        FOREIGN KEY (TOP_CD) REFERENCES D_TOP(TOP_CD)
    );
    CREATE TABLE F_SALES (SALES_ID INT PRIMARY KEY, MID_CD VARCHAR(10));
    """
    schema = DDLIntrospector(ddl).introspect()

    recovered = recover_relationships(schema)

    added = set(recovered.foreign_keys) - set(schema.foreign_keys)
    assert not any(fk.to_table == "D_MID" for fk in added)


def test_the_enterprise_fixture_still_folds_to_full_coverage():
    """골드셋 회귀: 복구 로직을 넓힌 뒤에도 커버리지가 유지되어야 한다."""
    from pathlib import Path

    from tablefold.t2sql.preset import fold_star_schema

    path = Path("fixtures/enterprise_bi.sql")
    if not path.exists():
        pytest.skip("픽스처 없음")
    schema = recover_relationships(DDLIntrospector.from_path(path).introspect())
    result = fold_star_schema(schema)
    assert result.layer.coverage >= 1.0
