"""Download Cricsheet limited-overs data (T20 + ODI, men's and women's) and build the DuckDB database."""
from __future__ import annotations

import argparse
import csv
import io
import shutil
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

import duckdb

from . import config
from .names import BOWL_KIND, TEAM_CANON, canon_competition, canon_team, canon_venue

# Cricsheet archive codes. International matches by type, plus club T20 leagues.
INTERNATIONAL = ["t20s", "it20s", "odis"]
CLUB_MALE = ["bbl", "bpl", "cpl", "ctc", "etpl", "ilt", "ipl", "ipt", "lpl", "mct", "mlc", "msl", "npl", "ntb",
             "pro20", "psl", "sat", "sma", "ssm"]
CLUB_FEMALE = ["blz", "cec", "frb", "ssm", "wbb", "wcl", "wpl", "wsl", "wtb", "wtc"]

RAW_COLUMNS = ["match_id", "season", "start_date", "venue", "innings", "ball", "actual_delivery", "batting_team",
               "bowling_team", "striker", "non_striker", "bowler", "runs_off_bat", "extras", "wides", "noballs",
               "byes", "legbyes", "penalty", "non_boundary", "wicket_type", "player_dismissed"]
WICKET_BOWLER = ("bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket")


def sources(scope: str = "all") -> list[str]:
    if scope == "ipl":
        return ["ipl_male"]
    return ([f"{c}_male" for c in INTERNATIONAL + CLUB_MALE] + [f"{c}_female" for c in INTERNATIONAL + CLUB_FEMALE])


def _ssl_context() -> ssl.SSLContext:
    try:  # some Python installs ship without system CA certificates; prefer certifi's bundle
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def download(force: bool = False, scope: str = "all") -> None:
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    ctx = _ssl_context()
    srcs = sources(scope)
    for i, code in enumerate(srcs, 1):
        dest = config.RAW_DIR / code
        if not force and any(dest.glob("*_info.csv")):
            continue
        url = f"{config.CRICSHEET_BASE}/{code}_csv2.zip"
        print(f"[{i}/{len(srcs)}] downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "datalens-ai/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
                data = r.read()
        except OSError as exc:
            raise SystemExit(f"Could not download {url} ({exc}).\nDownload it manually, unzip it into "
                             f"{dest}, then run this command again.") from None
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(dest)


def parse_info(path: Path) -> tuple[dict, list[tuple[str, str]]]:
    info: dict = {"teams": []}
    squad: list[tuple[str, str]] = []
    keys = ("winner", "winner_runs", "winner_wickets", "toss_winner", "toss_decision", "player_of_match", "city",
            "outcome", "method", "match_number", "event", "match_type", "gender", "team_type", "overs",
            "balls_per_over")
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 3 or row[0] != "info":
                continue
            key = row[1]
            if key == "team":
                info["teams"].append(row[2])
            elif key == "player" and len(row) >= 4:
                squad.append((row[2], row[3]))
            elif key in keys and key not in info:
                info[key] = row[2]
    return info, squad


def accept(info: dict) -> tuple[str, str] | None:
    """Return (format, competition) for matches we model, else None.

    We keep standard 6-ball T20 (20 overs) and ODI (50 overs) matches. Multi-day cricket and
    non-standard formats such as The Hundred are skipped because phases mean something different there.
    """
    mt, overs = info.get("match_type"), info.get("overs")
    if info.get("balls_per_over", "6") != "6":
        return None
    if mt in ("T20", "IT20") and overs == "20":
        fmt = "T20"
    elif mt == "ODI" and overs == "50":
        fmt = "ODI"
    else:
        return None
    if info.get("gender") not in ("male", "female"):
        return None
    comp = "International" if info.get("team_type") == "international" else canon_competition(info.get("event"))
    return fmt, comp


def build(db_path: Path = config.DB_PATH, scope: str = "all") -> Path:
    download(scope=scope)
    # Build into a temp file and swap it in only on success, so a failed run never leaves a half-built database.
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    con = duckdb.connect(str(tmp_path))

    # ---- matches + squads (info files) ----
    matches, squads, files, seen = [], [], [], set()
    for code in sources(scope):
        for p in sorted((config.RAW_DIR / code).glob("*_info.csv")):
            sid = p.stem.replace("_info", "")  # Cricsheet ids are mostly numeric but not always (e.g. wi_201706)
            data_file = p.with_name(f"{sid}.csv")
            if sid in seen or not data_file.exists():
                continue
            info, squad = parse_info(p)
            ok = accept(info)
            if not ok:
                continue
            seen.add(sid)
            files.append(str(data_file))
            fmt, comp = ok
            t = info["teams"] + [None, None]
            mid = len(matches) + 1  # dense internal match number
            matches.append((mid, sid, canon_team(t[0]), canon_team(t[1]), canon_team(info.get("toss_winner")),
                            info.get("toss_decision"), canon_team(info.get("winner")), info.get("winner_runs"),
                            info.get("winner_wickets"), info.get("player_of_match"), info.get("city"),
                            info.get("outcome"), info.get("method"), fmt, info["gender"],
                            info.get("team_type", "club"), comp, info.get("event")))
            squads += [(mid, canon_team(team), name) for team, name in squad]
    print(f"{len(matches)} matches accepted")
    con.execute("""CREATE TABLE matches_info(match_id BIGINT, source_id VARCHAR, team1 VARCHAR, team2 VARCHAR, toss_winner VARCHAR,
        toss_decision VARCHAR, winner VARCHAR, winner_runs VARCHAR, winner_wickets VARCHAR, player_of_match VARCHAR,
        city VARCHAR, outcome VARCHAR, method VARCHAR, fmt VARCHAR, gender VARCHAR, team_type VARCHAR,
        competition VARCHAR, event VARCHAR)""")
    con.executemany("INSERT INTO matches_info VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", matches)
    con.execute("CREATE TABLE match_players(match_id BIGINT, team VARCHAR, player VARCHAR)")
    con.executemany("INSERT INTO match_players VALUES (?,?,?)", squads)

    # ---- raw ball-by-ball CSVs, read in small batches (opening thousands of files at once exceeds the
    #      default open-file limit of 256 on macOS) ----
    cols = ", ".join(f"{c} VARCHAR" for c in RAW_COLUMNS)
    con.execute(f"CREATE TABLE raw_txt({cols})")
    for i in range(0, len(files), 50):
        batch = files[i:i + 50]
        rel = "read_csv(?, header=true, all_varchar=true, union_by_name=true)"
        present = {r[0] for r in con.execute(f"SELECT column_name FROM (DESCRIBE SELECT * FROM {rel})", [batch]).fetchall()}
        sel = ", ".join(c for c in RAW_COLUMNS if c in present)
        con.execute(f"INSERT INTO raw_txt BY NAME SELECT {sel} FROM {rel}", [batch])
        if (i // 50) % 20 == 0:
            print(f"  read {min(i + 50, len(files))}/{len(files)} match files")

    # ---- venue / team canonicalisation lookups ----
    raw_venues = [r[0] for r in con.execute("SELECT DISTINCT venue FROM raw_txt WHERE venue IS NOT NULL").fetchall()]
    con.execute("CREATE TABLE venue_map(raw VARCHAR, canon VARCHAR)")
    con.executemany("INSERT INTO venue_map VALUES (?,?)", [(v, canon_venue(v)) for v in raw_venues])
    con.execute("CREATE TABLE team_map(raw VARCHAR, canon VARCHAR)")
    con.executemany("INSERT INTO team_map VALUES (?,?)", list(TEAM_CANON.items()))

    # ---- typed ball-by-ball table (super overs excluded) ----
    con.execute("""CREATE TABLE raw AS
        SELECT *, row_number() OVER () AS seq FROM (
            SELECT mi.match_id AS match_id, season, CAST(start_date AS DATE) AS start_date, venue,
                   CAST(innings AS BIGINT) AS innings, CAST(ball AS DOUBLE) AS ball,
                   CAST(actual_delivery AS DOUBLE) AS actual_delivery, batting_team, bowling_team,
                   striker, non_striker, bowler, CAST(runs_off_bat AS BIGINT) AS runs_off_bat,
                   CAST(extras AS BIGINT) AS extras, CAST(wides AS BIGINT) AS wides,
                   CAST(noballs AS BIGINT) AS noballs, CAST(byes AS BIGINT) AS byes,
                   CAST(legbyes AS BIGINT) AS legbyes, CAST(penalty AS BIGINT) AS penalty,
                   TRY_CAST(non_boundary AS BOOLEAN) AS non_boundary, wicket_type, player_dismissed
            FROM raw_txt JOIN matches_info mi ON mi.source_id = raw_txt.match_id) WHERE innings <= 2""")
    con.execute("DROP TABLE raw_txt")

    # ---- batting position = order of arrival at the crease ----
    con.execute("""CREATE TABLE bat_pos AS
        WITH apps AS (
            SELECT match_id, innings, striker AS batter, seq * 2 AS k FROM raw
            UNION ALL
            SELECT match_id, innings, non_striker AS batter, seq * 2 + 1 AS k FROM raw),
        first_seen AS (SELECT match_id, innings, batter, min(k) AS fk FROM apps GROUP BY ALL)
        SELECT match_id, innings, batter,
               row_number() OVER (PARTITION BY match_id, innings ORDER BY fk) AS pos
        FROM first_seen""")

    wk = ",".join(f"'{w}'" for w in WICKET_BOWLER)
    con.execute(f"""CREATE TABLE balls AS
        WITH r AS (SELECT *, CAST(floor(actual_delivery) AS INTEGER) AS ov FROM raw)
        SELECT r.seq, r.match_id, year(r.start_date) AS season, r.start_date AS match_date,
               COALESCE(vm.canon, r.venue) AS venue, mi.fmt, mi.gender, mi.team_type, mi.competition,
               r.innings, r.ov AS over,
               CASE WHEN mi.fmt = 'T20' THEN (CASE WHEN r.ov < 6 THEN 'powerplay' WHEN r.ov < 15 THEN 'middle' ELSE 'death' END)
                    ELSE (CASE WHEN r.ov < 10 THEN 'powerplay' WHEN r.ov < 40 THEN 'middle' ELSE 'death' END) END AS phase,
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
               (r.runs_off_bat IN (4,6) AND NOT COALESCE(r.non_boundary, false)) AS is_boundary,
               bp.pos AS striker_pos
        FROM r
        JOIN matches_info mi ON mi.match_id = r.match_id
        LEFT JOIN venue_map vm ON vm.raw = r.venue
        LEFT JOIN team_map tb ON tb.raw = r.batting_team
        LEFT JOIN team_map tw ON tw.raw = r.bowling_team
        LEFT JOIN bat_pos bp ON bp.match_id = r.match_id AND bp.innings = r.innings AND bp.batter = r.striker""")
    con.execute("DROP TABLE raw")

    # ---- matches (one row per match, enriched) ----
    con.execute("""CREATE TABLE matches AS
        SELECT m.match_id, min(b.season) AS season, min(b.match_date) AS match_date, min(b.venue) AS venue,
               m.fmt, m.gender, m.team_type, m.competition, m.event,
               m.team1, m.team2, m.toss_winner, m.toss_decision, m.winner, m.winner_runs, m.winner_wickets,
               m.player_of_match, m.city, m.outcome, m.method,
               max(CASE WHEN b.innings=1 THEN b.batting_team END) AS first_bat_team,
               sum(CASE WHEN b.innings=1 THEN b.runs_off_bat+b.extras ELSE 0 END) AS inn1_runs,
               sum(CASE WHEN b.innings=2 THEN b.runs_off_bat+b.extras ELSE 0 END) AS inn2_runs
        FROM matches_info m JOIN balls b USING (match_id) GROUP BY ALL""")

    # ---- player metadata: every player, with hand-curated style data + country derived from internationals ----
    con.execute("""CREATE TEMP TABLE meta_csv AS SELECT * FROM read_csv(?, header=true, all_varchar=true)""",
                [str(config.META_CSV)])
    con.execute("""CREATE TABLE players_meta AS
        WITH everyone AS (SELECT striker AS player FROM balls UNION SELECT bowler FROM balls),
        reps AS (
            SELECT striker AS player, batting_team AS team FROM balls WHERE team_type = 'international'
            UNION ALL SELECT bowler, bowling_team FROM balls WHERE team_type = 'international'),
        cnt AS (SELECT player, regexp_replace(team, ' Women$', '') AS team, count(*) AS n FROM reps GROUP BY ALL),
        country AS (SELECT player, arg_max(team, n) AS country FROM cnt GROUP BY player)
        SELECT e.player,
               CASE WHEN c.nationality IN ('IND','OS') THEN c.nationality
                    WHEN k.country IS NULL THEN 'UNK'
                    WHEN k.country = 'India' THEN 'IND' ELSE 'OS' END AS nationality,
               k.country, NULLIF(c.bat_hand, '') AS bat_hand, NULLIF(c.bowl_style, '') AS bowl_style,
               CAST(NULL AS VARCHAR) AS bowl_kind
        FROM everyone e LEFT JOIN meta_csv c ON c.player = e.player LEFT JOIN country k ON k.player = e.player""")
    for style, kind in BOWL_KIND.items():
        con.execute("UPDATE players_meta SET bowl_kind = ? WHERE bowl_style = ?", [kind, style])

    for t in ("balls", "matches", "players_meta"):
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
    ap = argparse.ArgumentParser(description="Build the DataLens AI database from Cricsheet data.")
    ap.add_argument("--update", action="store_true",
                    help="re-download the latest Cricsheet data first (use after new matches have been played)")
    ap.add_argument("--scope", choices=["all", "ipl"], default="all",
                    help="'all' = T20 + ODI, men's and women's, internationals and ~30 leagues (default); "
                         "'ipl' = IPL only (small and fast, good for a quick demo)")
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep the extracted CSV files in data/raw (about 1 GB for --scope all)")
    args = ap.parse_args(argv)
    before = _match_count(config.DB_PATH)
    if args.update:
        download(force=True, scope=args.scope)
    build(scope=args.scope)
    after = _match_count(config.DB_PATH)
    if args.update and before is not None and after is not None:
        print(f"Matches: {before} -> {after} ({after - before:+d}). Restart the app to see the new data.")
    if not args.keep_raw and config.RAW_DIR.exists():
        shutil.rmtree(config.RAW_DIR, ignore_errors=True)
        print("Removed extracted CSVs (use --keep-raw to keep them).")


if __name__ == "__main__":
    main()
    sys.exit(0)
