"""Auction availability: tournament -> team -> squad, other teams' players excluded, yearly roster file."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from datalens.api import create_app
from datalens.engine import Engine, Filters
from datalens.rosters import OTHER_TEAMS, RosterError, RosterStore

IPL = Filters.from_dict({"competition": "ipl"})


@pytest.fixture()
def store(engine: Engine, tmp_path):
    return RosterStore(engine, tmp_path / "rosters.csv")


def test_teams_are_auto_guessed_from_latest_season(store):
    t = store.teams(IPL)
    names = {x["team"] for x in t["teams"]}
    assert {"Chennai Super Kings", "Mumbai Indians", "Royal Challengers Bengaluru"} <= names
    assert OTHER_TEAMS not in names
    assert all(x["source"].startswith("auto-guess") and x["players"] >= 10 for x in t["teams"])


def test_other_teams_players_are_unavailable_but_own_are_not(store):
    csk = set(store.team_squad(IPL, "csk")["players"])  # short names work
    u = store.unavailable(IPL, "Chennai Super Kings")
    assert u["other_teams"] == 9
    assert not (csk & u["players"])                       # own squad is not 'unavailable'
    mi = set(store.team_squad(IPL, "Mumbai Indians")["players"])
    assert mi <= u["players"]                             # everyone else's players are


def test_competition_is_required(store):
    with pytest.raises(RosterError):
        store.teams(Filters.from_dict({}))
    with pytest.raises(RosterError):
        store.team_squad(IPL, "Not A Team")


def test_saved_roster_overrides_auto_guess(store):
    store.save(IPL, 2027, "CSK", ["Ruturaj Gaikwad", "MS Dhoni"], free=["Jasprit Bumrah"], taken=["Rishabh Pant"])
    sq = store.team_squad(IPL, "Chennai Super Kings")
    assert sq["players"] == ["MS Dhoni", "RD Gaikwad"] and "roster file (2027)" in sq["source"]
    u = store.unavailable(IPL, "Chennai Super Kings")
    assert "RR Pant" in u["players"]            # marked unavailable
    assert "JJ Bumrah" not in u["players"]      # released -> free to buy
    other = store.team_squad(IPL, "Mumbai Indians")
    assert other["source"].startswith("auto-guess") and "JJ Bumrah" not in other["players"]


def test_trade_means_retained_by_new_team(store):
    store.save(IPL, 2027, "Mumbai Indians", ["JJ Bumrah", "Rohit Sharma"])
    u = store.unavailable(IPL, "Chennai Super Kings")
    assert "JJ Bumrah" in u["players"]


def test_free_list_overrides_other_teams(store):
    u = store.unavailable(IPL, "Chennai Super Kings", free=["Jasprit Bumrah"])
    assert "JJ Bumrah" not in u["players"]
    u = store.unavailable(IPL, "Chennai Super Kings", free=["zzzz nobody"])
    assert u["unresolved"] == ["zzzz nobody"]


def test_upload_validation_and_replace(store):
    with pytest.raises(RosterError):
        store.upload(pd.DataFrame({"team": ["x"]}))
    with pytest.raises(RosterError):
        store.upload(pd.DataFrame({"competition": ["ipl"], "season": ["2027"], "team": ["CSK"], "player": ["MS Dhoni"], "status": ["maybe"]}))
    df = pd.DataFrame({"competition": ["Indian Premier League"] * 2, "season": ["2027"] * 2, "team": ["CSK", ""],
                       "player": ["MS Dhoni", "Jasprit Bumrah"], "status": ["retained", "released"]})
    r = store.upload(df)
    assert r["rows"] == 2 and store.team_squad(IPL, "CSK")["players"] == ["MS Dhoni"]
    store.upload(df.assign(player=["Ruturaj Gaikwad", "Jasprit Bumrah"]))  # same season again replaces, not appends
    assert store.team_squad(IPL, "CSK")["players"] == ["RD Gaikwad"]


def test_api_planner_never_suggests_other_teams_players(engine, tmp_path):
    c = TestClient(create_app(engine, projects_dir=tmp_path, rosters_path=tmp_path / "r.csv"))
    body = {"purse": 40, "max_overseas": 3, "team": "CSK", "squad": ["Ruturaj Gaikwad", "MS Dhoni"],
            "filters": {"competition": "ipl", "last_n_seasons": 3},
            "slots": {"opener": 1, "middle_order": 1, "death_bowler": 2, "powerplay_bowler": 1}}
    r = c.post("/api/budget", json=body)
    assert r.status_code == 200
    out = r.json()
    other = RosterStore(engine, tmp_path / "x.csv").unavailable(IPL, "CSK")["players"]
    picked = {s["player"] for s in out["selected"]} | {a["player"] for a in out["alternatives"]}
    assert not (picked & other), picked & other
    assert out["roster"]["excluded_players"] > 100 and out["roster"]["other_teams"] == 9
    assert all(s["last_season"] >= 2025 for s in out["selected"])  # likely-retired players are skipped
    # without a competition the planner explains what to do
    bad = c.post("/api/budget", json={**body, "filters": {}})
    assert bad.status_code == 400 and "Competition" in bad.json()["detail"]
    # turning the filter off restores the old behaviour (other teams' stars allowed)
    off = c.post("/api/budget", json={**body, "use_roster": False})
    assert off.status_code == 200 and off.json()["roster"] is None


def test_api_roster_endpoints(engine, tmp_path):
    c = TestClient(create_app(engine, projects_dir=tmp_path, rosters_path=tmp_path / "r.csv"))
    assert c.get("/api/rosters/teams?competition=ipl").json()["teams"]
    assert c.get("/api/rosters/squad?competition=ipl&team=rcb").json()["players"]
    assert c.get("/api/rosters/squad?competition=ipl&team=nope").status_code == 400
    s = c.post("/api/rosters/save", json={"filters": {"competition": "ipl"}, "season": 2027, "team": "RCB",
                                          "retained": ["Virat Kohli"], "free": [], "taken": []})
    assert s.status_code == 200 and s.json()["saved"]["retained"] == 1
    assert c.get("/api/rosters/squad?competition=ipl&team=rcb").json()["players"] == ["V Kohli"]
