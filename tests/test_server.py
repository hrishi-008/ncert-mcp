"""The HTTP layer: secret path, 404 elsewhere, health, rate limit, and a real MCP round trip."""

import pytest
from starlette.testclient import TestClient

from server.main import Settings, create_app

TOKEN = "test-token-abcdefghijklmnop"
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client(tmp_path):
    from tests.conftest import BOOKS_DB
    s = Settings(secret_token=TOKEN, books_db=BOOKS_DB, state_db_url=f"sqlite:///{tmp_path}/state.db", rate_limit_per_min=30)
    with TestClient(create_app(s)) as c:
        yield c


def rpc(client, method, params=None, id_=1):
    r = client.post(f"/{TOKEN}/mcp", headers=H, json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("path", ["/", "/mcp", "/health", "/wrong-token/mcp", f"/{TOKEN[:-1]}/mcp", "/.env"])
def test_other_paths_are_404(client, path):
    assert client.get(path).status_code == 404
    assert client.post(path).status_code == 404


def test_health(client):
    body = client.get(f"/{TOKEN}/health").json()
    assert body["state_db"] == "ok"


def test_mcp_round_trip(client):
    init = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}})
    assert "NCERT" in init["result"]["instructions"]
    tools = {t["name"] for t in rpc(client, "tools/list", id_=2)["result"]["tools"]}
    assert tools == {"find_chapter", "list_chapters", "get_chapter_pack", "get_section", "search_books",
                     "get_teacher_notes", "update_teacher_notes", "log_paper", "get_past_questions"}
    r = rpc(client, "tools/call", {"name": "update_teacher_notes", "arguments": {
        "section": "corrections", "action": "add", "text": "Use new chapter names only", "reason": "test"}}, 3)
    assert not r["result"]["isError"]
    r = rpc(client, "tools/call", {"name": "get_teacher_notes", "arguments": {}}, 4)
    assert "Use new chapter names only" in r["result"]["content"][0]["text"]
    # Expected problems come back as readable errors, not crashes.
    r = rpc(client, "tools/call", {"name": "list_chapters", "arguments": {"class_": "9", "subject": "sci"}}, 5)
    assert r["result"]["isError"] and "Class 6 and Class 7" in r["result"]["content"][0]["text"]


def test_rate_limit(client):
    codes = [client.get(f"/{TOKEN}/health").status_code for _ in range(35)]
    assert 429 in codes


def test_settings_refuse_short_token(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "short")
    with pytest.raises(SystemExit):
        Settings.from_env()
