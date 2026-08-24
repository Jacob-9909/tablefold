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
