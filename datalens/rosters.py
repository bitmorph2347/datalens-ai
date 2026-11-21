"""Who is on which team: an automatic guess from the latest season plus a yearly roster file you maintain.

The auction planner needs to know which players are *available*. Cricsheet has match data only, so:

* **Auto-guess** - a team's squad is everyone who batted or bowled for it in the competition's latest season.
* **Roster file** (`data/rosters.csv`) - columns ``competition,season,team,player,status`` where status is
  ``retained`` or ``released``. After each retention / release / trade window, list the new retained squads here.
  For a team that has *any* retained row in the newest season of the file, the file replaces the auto-guess.
  A player marked ``released`` (any team) is free to buy. Trades are just a ``retained`` row under the new team.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import config
from .engine import Engine, Filters, _Q
from .names import canon_team

COLUMNS = ["competition", "season", "team", "player", "status"]
OTHER_TEAMS = "Other teams"  # holds players you mark as "taken" who are not on a listed squad


class RosterError(ValueError):
    pass


class RosterStore:
    def __init__(self, engine: Engine, path: Path | str = config.ROSTERS_CSV):
        self.e = engine
        self.path = Path(path)

    # ---------------------------------------------------------------- file
    def _read(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=COLUMNS)
        df = pd.read_csv(self.path, dtype=str).fillna("")
        missing = [c for c in COLUMNS if c not in df.columns]
        if missing:
            raise RosterError(f"{self.path.name} is missing columns: {', '.join(missing)}")
        return df[COLUMNS]

    def _write(self, df: pd.DataFrame) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        df[COLUMNS].to_csv(self.path, index=False)

    # ---------------------------------------------------------------- helpers
    def competition(self, f: Filters) -> str:
        """The exact competition name for these filters (rosters only make sense for one tournament)."""
        low = (f.competition or "").lower()
        if low not in self.e._comps:
            raise RosterError("Pick a tournament first: set Competition in the sidebar to e.g. IPL, SA20 or BBL.")
        return next(c for c in self._all_comps() if c.lower() == low)

    def _all_comps(self) -> list[str]:
        return [r[0] for r in self.e.query("SELECT DISTINCT competition FROM matches").values]

    def _resolve(self, names: list[str]) -> tuple[list[str], list[str]]:
        ok, bad = [], []
        for n in names:
            n = n.strip()
            if not n:
                continue
            try:
                ok.append(self.e.resolve_player_nl(n))
            except ValueError:
                bad.append(n)
        return list(dict.fromkeys(ok)), bad

    def latest_season(self, f: Filters) -> int:
        return self.e._seasons(f)[1]

    def auto_squads(self, f: Filters) -> dict[str, set[str]]:
        """Team -> players who featured in the competition's latest season."""
        latest = self.latest_season(f)
        g = Filters(format=f.format, gender=f.gender, competition=f.competition, season_from=latest, season_to=latest)
        q = _Q()
        cond = self.e._scope(g, q)
        df = self.e.query(f"""SELECT team, player FROM (
              SELECT batting_team AS team, striker AS player FROM balls WHERE {cond}
              UNION SELECT bowling_team, bowler FROM balls WHERE {cond}) GROUP BY ALL""", q.params)
        out: dict[str, set[str]] = {}
        for t, p in zip(df["team"], df["player"]):
            out.setdefault(t, set()).add(p)
        return out

    # ---------------------------------------------------------------- squads
    def squads(self, f: Filters) -> dict:
        comp = self.competition(f)
        auto = self.auto_squads(f)
        latest = self.latest_season(f)
        df = self._read()
        df = df[df.competition.str.lower() == comp.lower()]
        file_season = None
        retained: dict[str, set[str]] = {}
        released: set[str] = set()
        if len(df):
            file_season = int(df.season.astype(int).max())
            cur = df[df.season.astype(int) == file_season]
            for team, grp in cur[cur.status == "retained"].groupby("team"):
                retained[canon_team(team)] = set(grp.player)
            released = set(cur[cur.status == "released"].player)
        squads, source = {}, {}
        for team, players in auto.items():
            if team in retained:
                squads[team], source[team] = retained[team], f"roster file ({file_season})"
            else:
                squads[team], source[team] = {p for p in players if p not in released}, f"auto-guess from the {latest} season"
        for team, players in retained.items():
            if team not in squads:
                squads[team], source[team] = players, f"roster file ({file_season})"
        return {"competition": comp, "latest_season": latest, "file_season": file_season, "squads": squads,
                "source": source, "released": released}

    def teams(self, f: Filters) -> dict:
        s = self.squads(f)
        rows = [{"team": t, "players": len(p), "source": s["source"][t]} for t, p in sorted(s["squads"].items()) if t != OTHER_TEAMS]
        return {"competition": s["competition"], "latest_season": s["latest_season"], "file_season": s["file_season"],
                "teams": rows, "free_players": sorted(s["released"])}

    def team_squad(self, f: Filters, team: str) -> dict:
        s = self.squads(f)
        team = canon_team(team)
        match = next((t for t in s["squads"] if t.lower() == team.lower()), None)
        if not match:
            raise RosterError(f"'{team}' has no squad in {s['competition']}. Teams: {', '.join(sorted(s['squads']))}")
        return {"team": match, "players": sorted(s["squads"][match]), "source": s["source"][match],
                "competition": s["competition"], "free_players": sorted(s["released"])}

    def unavailable(self, f: Filters, my_team: str, free: list[str] | None = None, taken: list[str] | None = None) -> dict:
        """Players the planner must not suggest: everyone on another team's squad (plus ``taken``), minus ``free``."""
        s = self.squads(f)
        mine = canon_team(my_team)
        mine = next((t for t in s["squads"] if t.lower() == mine.lower()), mine)
        others: set[str] = set()
        for team, players in s["squads"].items():
            if team != mine:
                others |= players
        free_ok, free_bad = self._resolve(free or [])
        taken_ok, taken_bad = self._resolve(taken or [])
        others |= set(taken_ok)
        others -= set(free_ok)
        others -= s["squads"].get(mine, set()) & set(free_ok)
        return {"players": others, "source": s["source"], "latest_season": s["latest_season"],
                "file_season": s["file_season"], "unresolved": free_bad + taken_bad,
                "other_teams": len([t for t in s["squads"] if t not in (mine, OTHER_TEAMS)])}

    # ---------------------------------------------------------------- writing
    def save(self, f: Filters, season: int, team: str, retained: list[str], free: list[str] | None = None,
             taken: list[str] | None = None) -> dict:
        comp = self.competition(f)
        team = canon_team(team)
        ret_ok, ret_bad = self._resolve(retained)
        free_ok, free_bad = self._resolve(free or [])
        taken_ok, taken_bad = self._resolve(taken or [])
        df = self._read()
        same = (df.competition.str.lower() == comp.lower()) & (df.season == str(int(season)))
        df = df[~(same & ((df.team == team) | (df.status == "released") | (df.team == OTHER_TEAMS)))]
        new = [{"competition": comp, "season": str(int(season)), "team": team, "player": p, "status": "retained"} for p in ret_ok]
        new += [{"competition": comp, "season": str(int(season)), "team": OTHER_TEAMS, "player": p, "status": "retained"} for p in taken_ok]
        new += [{"competition": comp, "season": str(int(season)), "team": "", "player": p, "status": "released"} for p in free_ok]
        self._write(pd.concat([df, pd.DataFrame(new, columns=COLUMNS)], ignore_index=True))
        return {"saved": {"retained": len(ret_ok), "free": len(free_ok), "taken": len(taken_ok)}, "season": int(season),
                "team": team, "unresolved": ret_bad + free_bad + taken_bad, "file": self.path.name}

    def upload(self, df: pd.DataFrame) -> dict:
        df = df.copy()
        df.columns = [c.strip().lower() for c in df.columns]
        missing = [c for c in COLUMNS if c not in df.columns]
        if missing:
            raise RosterError(f"Roster file needs columns: {', '.join(COLUMNS)} (missing {', '.join(missing)})")
        df = df[COLUMNS].fillna("").astype(str)
        bad_status = sorted(set(df.status) - {"retained", "released"})
        if bad_status:
            raise RosterError(f"status must be 'retained' or 'released' (found: {', '.join(bad_status)})")
        names, unmatched = [], []
        for p in df.player:
            try:
                names.append(self.e.resolve_player_nl(p))
            except ValueError:
                names.append("")
                unmatched.append(p)
        df["player"] = names
        df = df[df.player != ""]
        comps = {c.lower(): c for c in self._all_comps()}
        df["competition"] = df.competition.map(lambda c: comps.get(c.strip().lower(), c.strip()))
        df["team"] = df.team.map(lambda t: canon_team(t.strip()) if t.strip() else "")
        old = self._read()
        keys = set(zip(df.competition.str.lower(), df.season))
        keep = old[[(c.lower(), s) not in keys for c, s in zip(old.competition, old.season)]]
        self._write(pd.concat([keep, df], ignore_index=True))
        return {"rows": len(df), "unmatched": unmatched, "competitions": sorted({c for c, _ in keys})}
