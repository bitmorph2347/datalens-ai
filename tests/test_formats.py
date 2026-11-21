"""Multi-format, multi-gender, multi-competition behaviour."""
import pytest

from datalens.engine import Engine, Filters, _Q

IPL = "Indian Premier League"


def test_phase_boundaries_differ_by_format(engine: Engine):
    df = engine.query("""SELECT fmt, phase, min(over) lo, max(over) hi FROM balls GROUP BY ALL""")
    got = {(r.fmt, r.phase): (r.lo, r.hi) for r in df.itertuples()}
    assert got[("T20", "powerplay")] == (0, 5) and got[("T20", "middle")] == (6, 14) and got[("T20", "death")] == (15, 19)
    assert got[("ODI", "powerplay")] == (0, 9) and got[("ODI", "middle")] == (10, 39) and got[("ODI", "death")] == (40, 49)


def test_formats_are_never_mixed(engine: Engine):
    t20 = engine.rank("bat", players=["V Kohli"], formula="runs", min_balls=100, filters={"format": "T20"})["rows"][0]
    odi = engine.rank("bat", players=["V Kohli"], formula="runs", min_balls=100, filters={"format": "ODI"})["rows"][0]
    assert t20["runs"] != odi["runs"]
    for fmt, row in (("T20", t20), ("ODI", odi)):
        n = engine.query(f"SELECT sum(runs_off_bat) r FROM balls WHERE striker='V Kohli' AND fmt='{fmt}' AND gender='male'").r[0]
        assert row["runs"] == n


def test_gender_separation(engine: Engine):
    women = engine.rank("bat", role="opener", filters={"gender": "female", "last_n_seasons": 3}, formula="strike_rate", limit=100)
    men = engine.rank("bat", role="opener", filters={"gender": "male", "last_n_seasons": 3}, formula="strike_rate", limit=100)
    w, m = {r["player"] for r in women["rows"]}, {r["player"] for r in men["rows"]}
    assert "S Mandhana" in w and "S Mandhana" not in m
    assert "V Kohli" in m and "V Kohli" not in w
    assert "women's" in women["filters_text"]


def test_competition_exact_beats_substring(engine: Engine):
    # 'Big Bash League' must not pull in the Women's Big Bash League
    q = _Q()
    cond = engine._scope(Filters.from_dict({"competition": "Big Bash League", "gender": "female"}), q, with_season=False)
    assert engine.query(f"SELECT count(*) c FROM balls WHERE {cond}", q.params).c[0] == 0
    q = _Q()
    cond = engine._scope(Filters.from_dict({"competition": "Big Bash", "gender": "female"}), q, with_season=False)
    assert engine.query(f"SELECT count(*) c FROM balls WHERE {cond}", q.params).c[0] > 0  # substring still works


def test_filter_aliases_and_validation():
    f = Filters.from_dict({"competition": "ipl", "format": "odi", "gender": "women"})
    assert (f.competition, f.format, f.gender) == (IPL, "ODI", "female")
    t = Filters.from_dict({"format": "t20i"})
    assert t.format == "T20" and t.team_type == "international"
    with pytest.raises(ValueError):
        Filters.from_dict({"format": "test"})
    with pytest.raises(ValueError):
        Filters.from_dict({"gender": "other"})
    with pytest.raises(ValueError):
        Filters.from_dict({"team_type": "school"})


def test_default_scope_is_t20_men():
    f = Filters.from_dict({})
    assert (f.format, f.gender) == ("T20", "male")
    assert "T20" in f.describe() and "men's" in f.describe()


def test_odi_roles_and_rankings(engine: Engine):
    r = engine.rank("bat", role="opener", filters={"format": "ODI", "last_n_seasons": 3}, formula="average", limit=10)
    assert r["rows"] and all(x["balls"] >= 300 for x in r["rows"])  # ODI default minimum sample is larger
    b = engine.bowling_roles(Filters(format="ODI", last_n_seasons=3))
    assert b.role_death_bowler.sum() > 5 and b.role_powerplay_bowler.sum() > 5


def test_derived_nationality(engine: Engine):
    meta = engine.query("SELECT player, nationality, country FROM players_meta").set_index("player")
    assert meta.loc["V Kohli", "nationality"] == "IND" and meta.loc["V Kohli", "country"] == "India"
    assert meta.loc["Babar Azam", "nationality"] == "OS" and meta.loc["Babar Azam", "country"] == "Pakistan"
    assert meta.loc["S Mandhana", "nationality"] == "IND"  # women's team names are normalised ("India Women" -> India)


def test_hundred_and_multiday_excluded(engine: Engine):
    names = engine.query("SELECT DISTINCT competition FROM matches").competition.str.lower()
    assert not names.str.contains("hundred|championship|shield|trophy.*first").any()
    assert set(engine.query("SELECT DISTINCT fmt FROM matches").fmt) == {"T20", "ODI"}


def test_active_players_follow_scope(engine: Engine):
    active = engine.active_players({"competition": IPL})
    assert "V Kohli" in active
    assert "KD Karthik" not in active  # retired from the IPL before the latest season
    assert "S Mandhana" not in active  # men's scope


def test_non_numeric_cricsheet_ids_are_loaded(engine: Engine):
    # some Cricsheet ids look like 'wi_201706'; they must still be loaded with a dense internal id
    assert engine.query("SELECT count(DISTINCT match_id) c FROM balls").c[0] == engine.query("SELECT count(*) c FROM matches").c[0]


def test_chat_scope_and_question_override(engine: Engine):
    from datalens.chat import ChatAgent
    a = ChatAgent(engine)
    out = a.ask("best openers by strike rate", scope={"format": "ODI", "gender": "female", "last_n_seasons": 3})
    assert "ODI" in out["answer"] and "women's" in out["answer"]
    out = a.ask("best openers by strike rate in T20Is", scope={"format": "ODI"})
    assert "T20" in out["answer"] and "international" in out["answer"]


def test_venue_profile_is_scoped(engine: Engine):
    men = engine.venue_profile("wankhede", {"format": "T20"})["venues"]
    odi = engine.venue_profile("wankhede", {"format": "ODI"})["venues"]
    assert men and men[0]["matches"] > 100
    assert not odi or odi[0]["avg_first_innings"] > men[0]["avg_first_innings"]
