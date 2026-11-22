"""Public deployment mode: private sessions, rate limits, caps and headers."""
import pytest
from fastapi.testclient import TestClient

from datalens.api import create_app

SID_A = "a" * 32
SID_B = "b" * 32


@pytest.fixture()
def client(engine, tmp_path):
    app = create_app(engine, projects_dir=tmp_path / "p", rosters_path=tmp_path / "r.csv", public=True,
                     session_dir=tmp_path / "sessions", rate_general=1000, rate_heavy=1000)
    return TestClient(app)


def h(sid):
    return {"X-Session-Id": sid}


def test_session_id_required_for_session_endpoints(client):
    assert client.get("/api/projects").status_code == 400
    assert client.get("/api/projects", headers=h("short")).status_code == 400
    assert client.get("/api/projects", headers=h("bad id with spaces!!!!!!!!!")).status_code == 400
    assert client.get("/api/projects", headers=h(SID_A)).status_code == 200
    # analytics that need no private state work without a session
    assert client.get("/api/meta").json()["public"] is True


def test_uploads_are_private_per_session(client):
    up = client.post("/api/upload", files={"file": ("mine.csv", b"a,b\n1,x\n2,y\n", "text/csv")}, headers=h(SID_A))
    assert up.status_code == 200
    assert client.get("/api/upload", headers=h(SID_A)).json()["tables"] == ["u_mine"]
    assert client.get("/api/upload", headers=h(SID_B)).json()["tables"] == []
    assert client.get("/api/upload/u_mine/profile", headers=h(SID_B)).status_code == 400
    assert client.post("/api/upload/sql", json={"query": "select * from u_mine"}, headers=h(SID_B)).status_code == 400


def test_projects_and_rosters_are_private_per_session(client):
    p = client.post("/api/projects", json={"name": "A's", "kind": "rank", "spec": {"kind": "rank"}}, headers=h(SID_A)).json()
    assert [x["id"] for x in client.get("/api/projects", headers=h(SID_A)).json()] == [p["id"]]
    assert client.get("/api/projects", headers=h(SID_B)).json() == []
    assert client.get(f"/api/projects/{p['id']}", headers=h(SID_B)).status_code == 404
    assert client.delete(f"/api/projects/{p['id']}", headers=h(SID_B)).status_code == 404
    save = client.post("/api/rosters/save", headers=h(SID_A), json={"filters": {"competition": "ipl"}, "season": 2027,
                       "team": "CSK", "retained": ["MS Dhoni"], "free": [], "taken": []})
    assert save.status_code == 200
    a = client.get("/api/rosters/squad?competition=ipl&team=csk", headers=h(SID_A)).json()
    b = client.get("/api/rosters/squad?competition=ipl&team=csk", headers=h(SID_B)).json()
    assert a["players"] == ["MS Dhoni"] and len(b["players"]) > 10 and b["source"].startswith("auto-guess")


def test_prices_upload_is_private(client):
    csv = b"player,price_cr\nVirat Kohli,18\n"
    assert client.post("/api/prices/upload", files={"file": ("p.csv", csv, "text/csv")}, headers=h(SID_A)).status_code == 200
    assert client.get("/api/prices", headers=h(SID_A)).json()["count"] == 1
    assert client.get("/api/prices", headers=h(SID_B)).json()["count"] > 100


def test_upload_size_cap_is_25mb(client):
    big = b"a,b\n" + b"1,xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n" * 600_000  # ~28 MB
    r = client.post("/api/upload", files={"file": ("big.csv", big, "text/csv")}, headers=h(SID_A))
    assert r.status_code in (413, 400) and "large" in r.text.lower()
    assert client.get("/api/meta").json()["max_upload_mb"] == 25


def test_upload_table_limit_per_session(client):
    for i in range(3):
        assert client.post("/api/upload", files={"file": (f"t{i}.csv", b"a\n1\n", "text/csv")}, headers=h(SID_A)).status_code == 200
    assert client.post("/api/upload", files={"file": ("t9.csv", b"a\n1\n", "text/csv")}, headers=h(SID_A)).status_code == 400


def test_raw_sql_and_docs_disabled(client):
    assert client.post("/api/sql", json={"query": "select 1"}, headers=h(SID_A)).status_code == 403
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_security_headers_and_no_store(client):
    r = client.get("/api/meta")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
    page = client.get("/")
    assert page.status_code == 200 and "Cricsheet" in page.text and "Open Data Commons Attribution" in page.text


def test_rate_limit_returns_429(engine, tmp_path):
    c = TestClient(create_app(engine, public=True, session_dir=tmp_path / "s", rate_general=5, rate_heavy=2))
    codes = [c.get("/api/meta").status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and 429 in codes[5:]
    c2 = TestClient(create_app(engine, public=True, session_dir=tmp_path / "s2", rate_general=100, rate_heavy=2))
    heavy = [c2.post("/api/chat", json={"message": "profile of kohli"}, headers=h(SID_A)).status_code for _ in range(4)]
    assert heavy[:2] == [200, 200] and 429 in heavy[2:]
    # a different client address has its own allowance
    other = c2.post("/api/chat", json={"message": "profile of kohli"}, headers={**h(SID_A), "X-Forwarded-For": "9.9.9.9"})
    assert other.status_code == 200


def test_health_and_oversized_request_rejected(client):
    assert client.get("/api/health").json()["status"] == "ok"
    r = client.post("/api/export/csv", content=b"x" * (5 * 1024 * 1024), headers={"content-type": "application/json", **h(SID_A)})
    assert r.status_code == 413


def test_sessions_are_capped_and_old_ones_cleaned_up(engine, tmp_path):
    app = create_app(engine, public=True, session_dir=tmp_path / "s", rate_general=1000, rate_heavy=1000, max_sessions=3)
    c = TestClient(app)
    for i in range(5):
        c.post("/api/projects", json={"name": f"p{i}", "kind": "k", "spec": {}}, headers=h(f"{i}" * 20))
    assert len(list((tmp_path / "s").iterdir())) <= 3  # oldest sessions were evicted and their files removed


def test_llm_disabled_in_public_mode(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("DATALENS_ALLOW_LLM", raising=False)
    c = TestClient(create_app(engine, public=True, session_dir=tmp_path / "s"))
    assert c.get("/api/meta").json()["chat_mode"] == "rules"


def test_local_mode_is_unchanged(engine, tmp_path):
    c = TestClient(create_app(engine, projects_dir=tmp_path / "p", rosters_path=tmp_path / "r.csv", public=False))
    assert c.get("/api/projects").status_code == 200  # no session header needed locally
    assert c.get("/api/meta").json()["max_upload_mb"] == 200
