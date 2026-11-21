"""Download Cricsheet IPL data and build the DuckDB analytics database."""
from __future__ import annotations

import argparse
import csv
import io
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

import duckdb

from . import config
from .names import BOWL_KIND, TEAM_CANON, canon_team, canon_venue

WICKET_BOWLER = ("bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket")


def download(force: bool = False) -> None:
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    if not force and any(config.RAW_DIR.glob("*_info.csv")):
        return
    print(f"Downloading {config.CRICSHEET_URL} ...")
    try:  # some Python installs ship without system CA certificates; prefer certifi's bundle
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(config.CRICSHEET_URL, headers={"User-Agent": "datalens-ai/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            data = r.read()
    except OSError as exc:
        raise SystemExit(f"Could not download the data ({exc}).\nDownload {config.CRICSHEET_URL} manually, "
                         f"unzip it into {config.RAW_DIR}, then run this command again.") from None
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(config.RAW_DIR)


def parse_info(path: Path) -> tuple[dict, list[tuple[str, str]]]:
    info: dict = {"teams": []}
    squad: list[tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 3 or row[0] != "info":
                continue
            key = row[1]
            if key == "team":
                info["teams"].append(row[2])
            elif key == "player" and len(row) >= 4:
                squad.append((row[2], row[3]))
            elif key in ("winner", "winner_runs", "winner_wickets", "toss_winner", "toss_decision",
                         "player_of_match", "city", "outcome", "method", "match_number", "event"):
                info[key] = row[2]
    return info, squad


def build(db_path: Path = config.DB_PATH) -> Path:
    download()
    # Build into a temp file and swap it in only on success, so a failed run never leaves a half-built database.
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    con = duckdb.connect(str(tmp_path))

    # ---- matches + squads ----
    matches, squads = [], []
    for p in sorted(config.RAW_DIR.glob("*_info.csv")):
        mid = int(p.stem.replace("_info", ""))
        info, squad = parse_info(p)
        t = info["teams"] + [None, None]
        matches.append((mid, canon_team(t[0]), canon_team(t[1]), canon_team(info.get("toss_winner")),
                        info.get("toss_decision"), canon_team(info.get("winner")),
                        info.get("winner_runs"), info.get("winner_wickets"), info.get("player_of_match"),
                        info.get("city"), info.get("outcome"), info.get("method")))
        squads += [(mid, canon_team(team), name) for team, name in squad]
    con.execute("""CREATE TABLE matches_info(match_id BIGINT, team1 VARCHAR, team2 VARCHAR, toss_winner VARCHAR,
        toss_decision VARCHAR, winner VARCHAR, winner_runs VARCHAR, winner_wickets VARCHAR,
        player_of_match VARCHAR, city VARCHAR, outcome VARCHAR, method VARCHAR)""")
    con.executemany("INSERT INTO matches_info VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", matches)
    con.execute("CREATE TABLE match_players(match_id BIGINT, team VARCHAR, player VARCHAR)")
    con.executemany("INSERT INTO match_players VALUES (?,?,?)", squads)

    # ---- venue / team canonicalisation lookups ----
    raw_venues = [r[0] for r in con.execute(
        f"SELECT DISTINCT venue FROM read_csv('{config.RAW_DIR}/*[0-9].csv', header=true, union_by_name=true, sample_size=-1)").fetchall()]
    con.execute("CREATE TABLE venue_map(raw VARCHAR, canon VARCHAR)")
    con.executemany("INSERT INTO venue_map VALUES (?,?)", [(v, canon_venue(v)) for v in raw_venues])
    con.execute("CREATE TABLE team_map(raw VARCHAR, canon VARCHAR)")
    con.executemany("INSERT INTO team_map VALUES (?,?)", list(TEAM_CANON.items()))

    # ---- raw ball-by-ball (super overs excluded) ----
    con.execute(f"""CREATE TABLE raw AS
        SELECT *, row_number() OVER () AS seq
        FROM read_csv('{config.RAW_DIR}/*[0-9].csv', header=true, auto_detect=true,
                      union_by_name=true, sample_size=-1)
        WHERE innings <= 2""")

    wk = ",".join(f"'{w}'" for w in WICKET_BOWLER)
    con.execute(f"""CREATE TABLE balls AS
        SELECT r.seq, r.match_id, year(r.start_date) AS season, r.start_date AS match_date,
               COALESCE(vm.canon, r.venue) AS venue, r.innings,
               CAST(floor(r.actual_delivery) AS INTEGER) AS over,
               CASE WHEN floor(r.actual_delivery) < 6 THEN 'powerplay'
                    WHEN floor(r.actual_delivery) < 15 THEN 'middle' ELSE 'death' END AS phase,
               COALESCE(tb.canon, r.batting_team) AS batting_team,
               COALESCE(tw.canon, r.bowling_team) AS bowling_team,
               r.striker, r.non_striker, r.bowler,
               r.runs_off_bat, COALESCE(r.extras,0) AS extras,
               COALESCE(r.wides,0) AS wides, COALESCE(r.noballs,0) AS noballs,
               COALESCE(r.byes,0) AS byes, COALESCE(r.legbyes,0) AS legbyes,
               r.wicket_type, r.player_dismissed,
               (COALESCE(r.wides,0)=0) AS batter_faced,
               (COALESCE(r.wides,0)=0 AND COALESCE(r.noballs,0)=0) AS legal_ball,
               (r.runs_off_bat + COALESCE(r.wides,0) + COALESCE(r.noballs,0)) AS runs_conceded,
               (r.wicket_type IN ({wk})) AS bowler_wicket,
               (r.wicket_type IS NOT NULL AND r.wicket_type NOT IN ('retired hurt','retired out')
                   AND r.player_dismissed = r.striker) AS striker_out,
               (r.runs_off_bat IN (4,6) AND NOT COALESCE(r.non_boundary, false)) AS is_boundary
        FROM raw r
        LEFT JOIN venue_map vm ON vm.raw = r.venue
        LEFT JOIN team_map tb ON tb.raw = r.batting_team
        LEFT JOIN team_map tw ON tw.raw = r.bowling_team""")
    con.execute("DROP TABLE raw")

    # ---- batting position = order of arrival at the crease ----
    con.execute("""CREATE TABLE bat_pos AS
        WITH apps AS (
            SELECT match_id, innings, striker AS batter, seq FROM balls
            UNION ALL
            SELECT match_id, innings, non_striker AS batter, seq FROM balls),
        first_seen AS (SELECT match_id, innings, batter, min(seq) AS first_seq FROM apps GROUP BY ALL)
        SELECT match_id, innings, batter,
               row_number() OVER (PARTITION BY match_id, innings ORDER BY first_seq,
                                  CASE WHEN batter IN (SELECT striker FROM balls b WHERE b.seq = first_seq) THEN 0 ELSE 1 END) AS pos
        FROM first_seen""")
    con.execute("ALTER TABLE balls ADD COLUMN striker_pos INTEGER")
    con.execute("""UPDATE balls SET striker_pos = bp.pos FROM bat_pos bp
        WHERE bp.match_id = balls.match_id AND bp.innings = balls.innings AND bp.batter = balls.striker""")

    # ---- matches (one row per match, enriched) ----
    con.execute("""CREATE TABLE matches AS
        SELECT m.match_id, min(b.season) AS season, min(b.match_date) AS match_date, min(b.venue) AS venue,
               m.team1, m.team2, m.toss_winner, m.toss_decision, m.winner, m.winner_runs, m.winner_wickets,
               m.player_of_match, m.city, m.outcome, m.method,
               max(CASE WHEN b.innings=1 THEN b.batting_team END) AS first_bat_team,
               sum(CASE WHEN b.innings=1 THEN b.runs_off_bat+b.extras ELSE 0 END) AS inn1_runs,
               sum(CASE WHEN b.innings=2 THEN b.runs_off_bat+b.extras ELSE 0 END) AS inn2_runs
        FROM matches_info m JOIN balls b USING (match_id) GROUP BY ALL""")

    # ---- player metadata ----
    con.execute("""CREATE TABLE players_meta AS SELECT player, nationality, bat_hand, bowl_style
        FROM read_csv(?, header=true, all_varchar=true)""", [str(config.META_CSV)])
    con.execute("ALTER TABLE players_meta ADD COLUMN bowl_kind VARCHAR")
    for style, kind in BOWL_KIND.items():
        con.execute("UPDATE players_meta SET bowl_kind = ? WHERE bowl_style = ?", [kind, style])

    for t in ("balls", "matches"):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n} rows")
    con.close()
    tmp_path.replace(db_path)
    return db_path


def _match_count(db_path: Path) -> int | None:
    if not db_path.exists():
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        n = con.execute("SELECT count(*) FROM matches").fetchone()[0]
        con.close()
        return n
    except duckdb.Error:
        return None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build the DataLens AI database from Cricsheet IPL data.")
    ap.add_argument("--update", action="store_true",
                    help="re-download the latest Cricsheet data first (use after new matches have been played)")
    args = ap.parse_args(argv)
    before = _match_count(config.DB_PATH)
    download(force=args.update)
    build()
    after = _match_count(config.DB_PATH)
    if args.update and before is not None and after is not None:
        print(f"Matches: {before} -> {after} ({after - before:+d}). Restart the app to see the new data.")


if __name__ == "__main__":
    main()
