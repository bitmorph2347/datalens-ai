"""Golden tests: results are checked against independent SQL / pandas calculations."""
import pytest

from datalens.engine import Engine, Filters


def test_data_loaded(engine: Engine):
    m = engine.meta()["counts"]
    assert m["matches"] > 1200 and m["balls"] > 290_000


def test_every_innings_has_two_openers(engine: Engine):
    df = engine.query("SELECT pos, count(*) c FROM bat_pos GROUP BY pos")
    c = dict(zip(df.pos, df.c))
    assert c[1] == c[2]  # one opener pair per innings


def test_kohli_is_top_run_scorer(engine: Engine):
    r = engine.rank("bat", formula="runs", min_balls=1000, limit=1)
    assert r["rows"][0]["player"] == "V Kohli"


def test_strike_rate_matches_independent_calculation(engine: Engine):
    r = engine.rank("bat", formula="strike_rate", players=["V Kohli"], min_balls=100)
    row = r["rows"][0]
    df = engine.query("""SELECT sum(runs_off_bat) runs, count(*) FILTER (WHERE wides = 0) balls
                         FROM balls WHERE striker = 'V Kohli'""")
    assert row["runs"] == df.runs[0] and row["balls"] == df.balls[0]
    assert row["strike_rate"] == pytest.approx(df.runs[0] * 100 / df.balls[0], abs=0.01)


def test_bowler_economy_excludes_byes_and_legbyes(engine: Engine):
    r = engine.rank("bowl", formula="economy", players=["JJ Bumrah"], min_balls=100)
    row = r["rows"][0]
    df = engine.query("""SELECT sum(runs_off_bat + wides + noballs) runs,
                         count(*) FILTER (WHERE wides = 0 AND noballs = 0) legal
                         FROM balls WHERE bowler = 'JJ Bumrah'""")
    assert row["runs"] == df.runs[0] and row["balls"] == df.legal[0]


# ---- the RR-hackathon requirement: role filters keep openers out of middle-order lists
def test_middle_order_list_contains_no_openers(engine: Engine):
    f = {"last_n_seasons": 3}
    mid = engine.rank("bat", role="middle_order", filters=f, formula="strike_rate", min_balls=150, limit=50)
    roles = engine.batting_roles(Filters(last_n_seasons=3)).set_index("player")
    assert mid["rows"], "expected some middle-order batters"
    for row in mid["rows"]:
        assert roles.loc[row["player"], "share_middle_order"] >= 0.6
        assert roles.loc[row["player"], "share_opener"] < 0.4  # openers are excluded


def test_openers_list_has_openers_only(engine: Engine):
    op = engine.rank("bat", role="opener", filters={"last_n_seasons": 3}, formula="strike_rate", limit=50)
    names = [r["player"] for r in op["rows"]]
    assert "V Kohli" in names or "Shubman Gill" in names
    assert "MS Dhoni" not in names


def test_death_bowler_role_and_phase(engine: Engine):
    r = engine.rank("bowl", role="death_bowler", filters={"last_n_seasons": 3, "phase": "death"},
                    formula="economy", min_balls=80, limit=10)
    assert r["rows"][0]["player"] == "JJ Bumrah"
    econ = [x["economy"] for x in r["rows"]]
    assert econ == sorted(econ)  # lower economy is better -> ascending


def test_powerplay_bowler_filter_is_phase_aware(engine: Engine):
    r = engine.rank("bowl", role="powerplay_bowler", filters={"last_n_seasons": 3, "phase": "powerplay"}, limit=40)
    assert len(r["rows"]) > 10
    assert all(x["balls"] >= 60 for x in r["rows"])


def test_custom_formula_zscore_is_centred(engine: Engine):
    r = engine.rank("bat", formula="0.5*z(strike_rate)+0.5*z(average)", filters={"last_n_seasons": 3}, limit=200)
    scores = [x["score"] for x in r["rows"]]
    assert abs(sum(scores) / len(scores)) < 0.35  # roughly centred (nulls drop some rows)


def test_bad_formula_is_rejected(engine: Engine):
    with pytest.raises(ValueError):
        engine.rank("bat", formula="__import__('os')")


def test_unknown_role_rejected(engine: Engine):
    with pytest.raises(ValueError):
        engine.rank("bat", role="wicketkeeper")


def test_min_balls_guard(engine: Engine):
    r = engine.rank("bat", formula="strike_rate", min_balls=500, limit=100)
    assert all(x["balls"] >= 500 for x in r["rows"])


def test_head_to_head_matches_sql(engine: Engine):
    h = engine.head_to_head("kohli", "bumrah")
    n = engine.query("SELECT count(*) FILTER (WHERE wides = 0) c FROM balls WHERE striker='V Kohli' AND bowler='JJ Bumrah'").c[0]
    assert h["total"]["balls"] == n and h["batter"] == "V Kohli"


def test_pace_spin_split_sums_to_total(engine: Engine):
    r = engine.batter_vs_bowler_types("V Kohli", {"last_n_seasons": 3})
    total = sum(x["balls"] for x in r["by_kind"])
    n = engine.query("SELECT count(*) FILTER (WHERE wides = 0) c FROM balls WHERE striker='V Kohli' AND season >= 2024").c[0]
    assert total == n


def test_bowler_kind_filter_uses_only_known_bowlers(engine: Engine):
    r = engine.rank("bat", filters={"last_n_seasons": 3, "bowler_kind": "spin"}, formula="strike_rate", min_balls=100)
    assert r["rows"] and any("style data" in w for w in r["warnings"])


def test_venue_profile_and_name_normalisation(engine: Engine):
    v = engine.venue_profile("wankhede")["venues"]
    assert len(v) == 1 and v[0]["venue"].startswith("Wankhede") and v[0]["matches"] > 100
    assert 120 < v[0]["avg_first_innings"] < 220


def test_venue_aliases_merged(engine: Engine):
    names = [x["venue"] for x in engine.venue_profile()["venues"]]
    assert sum("Chinnaswamy" in n for n in names) == 1
    assert sum("Kotla" in n or "Arun Jaitley" in n for n in names) == 1


def test_team_names_canonicalised(engine: Engine):
    teams = engine.meta()["teams"]
    assert "Delhi Daredevils" not in teams and "Delhi Capitals" in teams
    assert "Royal Challengers Bangalore" not in teams


def test_name_resolution_prefers_famous_player(engine: Engine):
    assert engine.resolve_player("kohli") == "V Kohli"
    assert engine.resolve_player("Rohit Sharma") == "RG Sharma"
    assert engine.resolve_player("dhoni") == "MS Dhoni"
    with pytest.raises(ValueError):
        engine.resolve_player("zzzzqqqq")


def test_squad_gaps(engine: Engine):
    g = engine.squad_gaps(["Virat Kohli", "Jasprit Bumrah", "Nobody Real"])
    assert g["unresolved"] == ["Nobody Real"]
    assert g["gaps"]["death_bowler"] >= 1 or g["gaps"]["powerplay_bowler"] >= 1


def test_filters_validation():
    with pytest.raises(ValueError):
        Filters.from_dict({"phase": "lunch"})
    with pytest.raises(ValueError):
        Filters.from_dict({"nonsense": 1})
    assert Filters.from_dict({"team": "Delhi Daredevils"}).team == "Delhi Capitals"


def test_no_sql_injection_via_filters(engine: Engine):
    r = engine.rank("bat", filters={"venue": "x'; DROP TABLE balls; --"}, formula="runs")
    assert r["rows"] == []
    assert engine.query("SELECT count(*) c FROM balls").c[0] > 0
