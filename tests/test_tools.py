"""Similarity, budget optimiser, chat parser, uploads and API."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from datalens import budget, similarity
from datalens.api import create_app
from datalens.chat import ChatAgent, parse_question, safe_select
from datalens.uploads import UploadStore


@pytest.fixture(scope="session")
def prices(engine, tmp_path_factory):
    df = budget.make_sample_prices(engine, path=tmp_path_factory.mktemp("prices") / "prices.csv")
    return budget.load_prices(engine, df=df)[0]


# ---------------------------------------------------------------- similarity
def test_similarity_excludes_self_and_sorted(engine):
    r = similarity.similar_players(engine, "Hardik Pandya", "bat", n=6)
    names = [x["player"] for x in r["rows"]]
    assert "HH Pandya" not in names and len(names) == 6
    sims = [x["similarity"] for x in r["rows"]]
    assert sims == sorted(sims, reverse=True)


def test_similarity_respects_nationality_and_candidates(engine):
    r = similarity.similar_players(engine, "Bumrah", "bowl", n=5, nationality="OS")
    assert r["rows"] and all(x["nationality"] == "OS" for x in r["rows"])
    r2 = similarity.similar_players(engine, "Bumrah", "bowl", n=5, candidates=["Arshdeep Singh", "B Kumar"])
    assert {x["player"] for x in r2["rows"]} <= {"Arshdeep Singh", "B Kumar"}


def test_similarity_small_sample_rejected(engine):
    with pytest.raises(ValueError):
        similarity.similar_players(engine, "MS Dhoni", "bowl")


# ---------------------------------------------------------------- budget
def test_budget_respects_all_constraints(engine, prices):
    slots = {"opener": 1, "middle_order": 2, "death_bowler": 1, "powerplay_bowler": 1}
    r = budget.optimize(engine, prices, purse=60, slots=slots, max_overseas=2)
    sel = pd.DataFrame(r["selected"])
    assert r["spent"] <= 60 and sel.price.sum() <= 60 + 1e-6
    assert (sel.nationality == "OS").sum() <= 2
    assert sel.player.is_unique
    assert sel.groupby("role").size().to_dict() == slots


def test_budget_is_optimal_vs_greedy(engine, prices):
    tight = budget.optimize(engine, prices, purse=15, slots={"opener": 1, "death_bowler": 1}, max_overseas=2)
    rich = budget.optimize(engine, prices, purse=80, slots={"opener": 1, "death_bowler": 1}, max_overseas=2)
    assert rich["total_score"] >= tight["total_score"]


def test_budget_infeasible_purse_gives_partial_with_warning(engine, prices):
    r = budget.optimize(engine, prices, purse=0.5, slots={"opener": 2, "middle_order": 2}, max_overseas=2)
    assert not r["optimal"] and r["unfilled"] and r["warnings"]


def test_budget_only_active_players(engine, prices):
    active = engine.active_players()
    r = budget.optimize(engine, prices, purse=80, slots={"middle_order": 3}, max_overseas=3)
    assert all(s["player"] in active for s in r["selected"])


def test_price_upload_fuzzy_names(engine):
    df = pd.DataFrame({"player": ["Virat Kohli", "Jasprit Bumrah", "Totally Unknown"], "price_cr": [21, 18, 1]})
    out, unmatched = budget.load_prices(engine, df=df)
    assert set(out.player) == {"V Kohli", "JJ Bumrah"} and unmatched == ["Totally Unknown"]


# ---------------------------------------------------------------- chat parser (rules mode)
@pytest.mark.parametrize("q,tool,check", [
    ("best middle order batters by strike rate since 2022", "rank_players",
     lambda a: a["role"] == "middle_order" and a["side"] == "bat" and a["filters"]["season_from"] == 2022),
    ("top 10 death bowlers by economy in the last 3 seasons", "rank_players",
     lambda a: a["role"] == "death_bowler" and a["filters"]["phase"] == "death" and a["order"] == "asc"
     and a["filters"]["last_n_seasons"] == 3),
    ("top openers by average at Wankhede", "rank_players",
     lambda a: a["role"] == "opener" and "Wankhede" in a["filters"]["venue"]),
    ("best finishers against spin since 2023", "rank_players",
     lambda a: a["role"] == "finisher" and a["filters"]["bowler_kind"] == "spin"),
    ("Kohli vs spin", "batter_vs_bowler_types", lambda a: a["batter"] == "V Kohli"),
    ("Kohli vs Bumrah", "head_to_head", lambda a: (a["batter"], a["bowler"]) == ("V Kohli", "JJ Bumrah")),
    ("Bumrah against left handers", "bowler_vs_batter_hands", lambda a: a["bowler"] == "JJ Bumrah"),
    ("players similar to Hardik Pandya", "similar_players", lambda a: a["player"] == "HH Pandya"),
    ("venue profile for Chepauk", "venue_profile", lambda a: "Chidambaram" in a["venue"]),
    ("profile of MS Dhoni", "player_profile", lambda a: a["player"] == "MS Dhoni"),
])
def test_rules_parser(engine, q, tool, check):
    name, args = parse_question(engine, q)
    assert name == tool and check(args), (q, name, args)


def test_rules_chat_end_to_end(engine):
    out = ChatAgent(engine).ask("top 5 powerplay bowlers by economy since 2023")
    assert out["mode"] == "rules" and out["tables"][0]["rows"] and out["steps"][0]["tool"] == "rank_players"


def test_unknown_question_returns_help(engine):
    assert "I can answer questions like" in ChatAgent(engine).ask("what's the weather")["answer"]


class _FakeClient:
    """Mimics the Anthropic client: first asks for a tool call, then answers."""

    class _Msgs:
        def __init__(self):
            self.n = 0

        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                blk = type("B", (), {"type": "tool_use", "name": "rank_players", "id": "t1",
                                     "input": {"side": "bat", "role": "opener", "formula": "strike_rate",
                                               "filters": {"last_n_seasons": 2}, "limit": 3}})()
                return type("R", (), {"stop_reason": "tool_use", "content": [blk]})()
            txt = type("T", (), {"type": "text", "text": "Done."})()
            return type("R", (), {"stop_reason": "end_turn", "content": [txt]})()

    def __init__(self):
        self.messages = self._Msgs()


def test_claude_mode_tool_loop(engine):
    out = ChatAgent(engine, client=_FakeClient()).ask("who opens best?")
    assert out["mode"] == "claude" and out["answer"] == "Done." and out["tables"][0]["rows"]


@pytest.mark.parametrize("bad", [
    "select 1; drop table balls", "drop table balls", "copy balls to 'x.csv'", "select * from read_csv('/etc/passwd')",
    "delete from balls", "attach 'x.db'", "pragma database_list", "install httpfs", "insert into balls select 1",
    "update balls set season = 0", "create table x as select 1", "",
])
def test_sql_guard_blocks(bad):
    with pytest.raises(ValueError):
        safe_select(bad)


def test_sql_guard_allows_select_and_cte(engine):
    assert engine.query(safe_select("with x as (select season from matches) select count(*) c from x")).c[0] > 1000


def test_database_is_read_only(engine):
    with pytest.raises(Exception):
        engine.query("DELETE FROM balls")
    with pytest.raises(Exception):
        engine.query("SELECT * FROM read_csv('/etc/hosts')")


# ---------------------------------------------------------------- uploads
def test_upload_profile_and_clean():
    u = UploadStore()
    csv = b"Name,Runs,Team\n Ravi ,10,india\nRavi,10,India\nAmit,,India\nSam,5,Pak\nRavi,10,India\n"
    p = u.add("My Data.csv", csv)
    assert p["table"] == "u_my_data" and p["duplicates"] >= 1 and p["issues"]
    c = u.clean("u_my_data", fill="median")
    assert c["rows_after"] < c["rows_before"] and c["duplicates"] == 0
    assert u.query("SELECT count(*) AS n FROM u_my_data")["rows"][0]["n"] == c["rows_after"]
    with pytest.raises(ValueError):
        u.query("SELECT * FROM read_csv('/etc/passwd')")


def test_upload_rejects_bad_files():
    u = UploadStore()
    with pytest.raises(ValueError):
        u.add("x.exe", b"abc")
    with pytest.raises(ValueError):
        u.add("empty.csv", b"")


# ---------------------------------------------------------------- API
@pytest.fixture(scope="module")
def client(engine, tmp_path_factory):
    return TestClient(create_app(engine, projects_dir=tmp_path_factory.mktemp("projects")))


def test_api_rank_and_errors(client):
    r = client.post("/api/rank", json={"side": "bat", "role": "middle_order", "filters": {"last_n_seasons": 3}, "limit": 5})
    assert r.status_code == 200 and len(r.json()["rows"]) == 5
    bad = client.post("/api/rank", json={"side": "bat", "formula": "os.system('x')"})
    assert bad.status_code == 400
    assert client.post("/api/rank", json={"side": "bat", "filters": {"phase": "lunch"}}).status_code == 400


def test_api_sql_endpoint_is_guarded(client):
    assert client.post("/api/sql", json={"query": "select count(*) c from matches"}).status_code == 200
    assert client.post("/api/sql", json={"query": "drop table balls"}).status_code == 400


def test_api_budget_from_squad_gaps(client):
    r = client.post("/api/budget", json={"purse": 80, "squad": ["Virat Kohli", "Jasprit Bumrah"], "max_overseas": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["spent"] <= 80 and body["derived_gaps"]["gaps"]
    assert "Virat Kohli" not in [s["player"] for s in body["selected"]]


def test_api_projects_crud_and_exports(client):
    p = client.post("/api/projects", json={"name": "demo", "kind": "rank", "spec": {"kind": "rank"}}).json()
    assert client.get(f"/api/projects/{p['id']}").json()["name"] == "demo"
    assert any(x["id"] == p["id"] for x in client.get("/api/projects").json())
    assert client.delete(f"/api/projects/{p['id']}").status_code == 200
    assert client.get(f"/api/projects/{p['id']}").status_code == 404
    assert client.get("/api/projects/../../etc/passwd").status_code in (400, 404)
    csv = client.post("/api/export/csv", json={"columns": ["a", "b"], "rows": [{"a": 1, "b": "x"}]})
    assert csv.text.splitlines() == ["a,b", "1,x"]
    html = client.post("/api/export/report", json={"title": "T", "columns": ["a"], "rows": [{"a": "<script>"}]}).text
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_api_upload_flow(client):
    up = client.post("/api/upload", files={"file": ("t.csv", b"a,b\n1,x\n2,y\n", "text/csv")})
    assert up.status_code == 200
    assert client.post("/api/upload/sql", json={"query": "select sum(a) s from u_t"}).json()["rows"][0]["s"] == 3
    assert client.get("/api/upload/u_t/chart?x=b").status_code == 200
    assert client.get("/api/upload/u_t/chart?x=nope").status_code == 400
