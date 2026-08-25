"""데모 화면 뒤의 엔드포인트.

핵심 파이프라인이 지키는 규칙이 화면에서도 지켜지는지 본다 — 특히 "없으면
없다고 말하는" 것(LLM 키)과, 안내 문구가 실제 결과와 어긋나지 않는 것(방언).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from demo.app import _resolve_chat_dialect, app

client = TestClient(app)


def test_chat_capability_false_without_keys(monkeypatch):
    """키가 없으면 챗봇은 답할 수 없다. 화면이 미리 알아야 입력을 막는다."""
    from tablefold.t2sql.provider import ProviderUnavailable

    def _no_provider(*args, **kwargs):
        raise ProviderUnavailable("OPENAI_API_KEY 가 설정되어 있지 않다")

    monkeypatch.setattr(
        "tablefold.t2sql.provider.default_completer", _no_provider
    )
    res = client.get("/api/chat-capability")
    assert res.status_code == 200
    assert res.json() == {"llm_available": False}


def test_chat_capability_true_when_provider_exists(monkeypatch):
    monkeypatch.setattr(
        "tablefold.t2sql.provider.default_completer", lambda *a, **k: lambda p: ""
    )
    res = client.get("/api/chat-capability")
    assert res.status_code == 200
    assert res.json() == {"llm_available": True}


def test_chat_dialect_live_resolves_to_connection_dialect():
    # 라이브 소스의 접속 대상은 MSSQL 이다. postgres 로 물어도 접속 대상
    # 문법으로 바뀌어야 한다 — 그렇지 않으면 LIMIT 이 섞여 실행 단계에서 죽는다.
    assert _resolve_chat_dialect("live", "postgres") == "tsql"
    assert _resolve_chat_dialect("financial", "") == "tsql"


def test_chat_dialect_offline_stays_postgres():
    # 예제(DDL) 소스는 데이터베이스가 없으므로 PostgreSQL 이 기본이다.
    # 라이브 치환을 그대로 적용했던 동안 화면의 "PostgreSQL 문법으로 답합니다"
    # 안내와 다른 T-SQL 이 나왔다.
    assert _resolve_chat_dialect("ddl", "") == "postgres"
    assert _resolve_chat_dialect("ddl", "postgres") == "postgres"


# ── 확장 경고가 화면까지 살아남나 ─────────────────────────────────────────────
#
# 경고 사례(자식 집계 간 키 불일치)는 실행해도 에러가 나지 않는다. API 가 그
# 경고를 잃어버리면 화면은 치우친 숫자를 경고 없이 보여 준다.

TWO_CHILDREN_DDL = """
CREATE TABLE orders (
    order_id BIGINT PRIMARY KEY,
    region_cd VARCHAR(20),
    amount NUMERIC
);
CREATE TABLE order_items (
    line_id BIGINT PRIMARY KEY,
    order_id BIGINT,
    qty INTEGER,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
CREATE TABLE order_notes (
    note_id BIGINT PRIMARY KEY,
    order_id BIGINT,
    region_cd VARCHAR(20),
    score INTEGER,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (region_cd) REFERENCES orders(region_cd)
);
"""

CHAT_STAR_DDL = """
CREATE TABLE D_ORG (
    ORG_CD VARCHAR(100) PRIMARY KEY,
    HEAD_NM VARCHAR(100)
);
CREATE TABLE D_ITEM (
    ITEM_CD VARCHAR(100) PRIMARY KEY,
    ITEM_NM VARCHAR(100)
);
CREATE TABLE F_SALES (
    YYYYMMDD VARCHAR(8),
    ORG_CD VARCHAR(100),
    ITEM_CD VARCHAR(100),
    SALES_AMT NUMERIC(18,2),
    PRIMARY KEY (YYYYMMDD, ORG_CD, ITEM_CD),
    FOREIGN KEY (ORG_CD) REFERENCES D_ORG(ORG_CD),
    FOREIGN KEY (ITEM_CD) REFERENCES D_ITEM(ITEM_CD)
);
"""


def _replying(text: str):
    """고정 답 하나만 돌려주는 completer."""
    return lambda _prompt: text


def _offline_database(monkeypatch):
    """.env 의 실제 접속 정보(MSSQL 도커)를 따라가지 않게 끊는다.
    이 테스트들의 관심사는 응답 형태지 데이터베이스가 아니다."""
    monkeypatch.setattr("demo.live.available", lambda: False)
    monkeypatch.setattr(
        "demo.live.execute_query", lambda sql: {"status": "not_connected"}
    )


def test_expand_response_carries_expansion_warnings():
    """범위가 섞인 확장은 성공(200)하지만 경고를 함께 실려 나간다."""
    res = client.post(
        "/api/expand",
        json={
            "ddl": TWO_CHILDREN_DDL,
            "source": "ddl",
            "sql": (
                "SELECT order_items_qty_sum, region_order_notes_score_sum "
                "FROM orders"
            ),
        },
    )
    assert res.status_code == 200
    warnings = res.json()["warnings"]
    assert any("범위가 다릅니다" in w for w in warnings)


def test_expand_response_stays_quiet_when_children_align():
    """같은 부모 컬럼에 붙은 자식들끼리는 경고가 소음이 된다."""
    res = client.post(
        "/api/expand",
        json={
            "ddl": TWO_CHILDREN_DDL,
            "source": "ddl",
            "sql": "SELECT order_items_qty_sum FROM orders",
        },
    )
    assert res.status_code == 200
    assert res.json()["warnings"] == []


def test_chat_response_surfaces_grain_warning(monkeypatch):
    """챗봇 응답도 경고를 잃지 않는다.

    ``GenerationResult`` 는 경고를 실어 보내지 않으므로 화면 API 가 최종 논리
    SQL 을 같은 레이어로 다시 확장해 얻는다. 같은 입력이므로 같은 경고가
    결정적으로 나온다.
    """
    monkeypatch.setattr(
        "tablefold.t2sql.default_completer",
        lambda *a, **k: _replying(
            "```sql\nSELECT order_items_qty_sum, "
            "region_order_notes_score_sum FROM orders\n```"
        ),
    )
    _offline_database(monkeypatch)

    res = client.post(
        "/api/chat",
        json={
            "question": "수량 합계와 점수 합계",
            "source": "ddl",
            "ddl": TWO_CHILDREN_DDL,
        },
    )
    assert res.status_code == 200
    warnings = res.json()["warnings"]
    assert any("범위가 다릅니다" in w for w in warnings)


def test_chat_response_keeps_empty_warnings_without_caution(monkeypatch):
    """주의할 것이 없으면 빈 목록이라도 키는 있다 — 화면이 키의 유무로
    정상/비정상을 가르면 안 되기 때문이다."""
    monkeypatch.setattr(
        "tablefold.t2sql.default_completer",
        # 스타 레이어의 앵커는 팩트 하나다. 차원 표는 흡수돼 모델이 아니므로
        # F_SALES 를 읽는 질문만 통과한다.
        lambda *a, **k: _replying("```sql\nSELECT HEAD_NM FROM F_SALES\n```"),
    )
    _offline_database(monkeypatch)

    res = client.post(
        "/api/chat",
        json={
            "question": "본부 이름을 알려줘",
            "source": "ddl",
            "ddl": CHAT_STAR_DDL,
        },
    )
    assert res.status_code == 200
    assert res.json()["warnings"] == []
