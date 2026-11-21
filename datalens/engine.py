"""Core analytics engine: roles, rankings with custom formulas, matchups, venues, squad gaps."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

import duckdb
import pandas as pd

from . import config
from .formula import FormulaError, compile_formula
from .names import PlayerResolver, canon_team, canon_venue

# ------------------------------------------------------------------ metrics
BAT_METRICS = {
    "innings": "Innings batted", "runs": "Runs", "balls": "Balls faced", "dismissals": "Times dismissed",
    "fours": "Fours", "sixes": "Sixes", "dots": "Dot balls faced",
    "strike_rate": "Runs per 100 balls", "average": "Runs per dismissal",
    "boundary_pct": "% of balls hit for 4/6", "dot_pct": "% of balls that were dots",
    "balls_per_dismissal": "Balls faced per dismissal", "runs_per_innings": "Runs per innings",
    "boundary_runs_pct": "% of runs from boundaries",
}
BOWL_METRICS = {
    "innings": "Innings bowled", "balls": "Legal balls bowled", "overs": "Overs bowled", "runs": "Runs conceded",
    "wickets": "Wickets (bowler-credited)", "dots": "Dot balls", "fours": "Fours conceded", "sixes": "Sixes conceded",
    "wides": "Wides", "noballs": "No-balls",
    "economy": "Runs per over", "bowling_average": "Runs per wicket", "bowling_sr": "Balls per wicket",
    "dot_pct": "% dot balls", "boundary_pct": "% balls hit for 4/6", "wickets_per_over": "Wickets per over",
}
LOWER_IS_BETTER = {"economy", "bowling_average", "bowling_sr", "boundary_pct_conceded"}

BAT_ROLES = {
    "opener": "Usually bats at 1-2",
    "top_order": "Usually bats at 1-3",
    "middle_order": "Usually bats at 4-6",
    "finisher": "Bats 4-7 and faces a large share of balls in overs 16-20",
    "lower_order": "Usually bats at 7 or lower",
}
BOWL_ROLES = {
    "powerplay_bowler": "Bowls at least 40% of their balls in overs 1-6",
    "middle_overs_bowler": "Bowls at least 50% of their balls in overs 7-15",
    "death_bowler": "Bowls at least 30% of their balls in overs 16-20",
    "pacer": "Fast / medium bowler (needs players_meta)",
    "spinner": "Spin bowler (needs players_meta)",
}
ROLE_ALIASES = {
    "opening": "opener", "openers": "opener", "top": "top_order", "top order": "top_order",
    "middle": "middle_order", "middle order": "middle_order", "finishers": "finisher",
    "lower": "lower_order", "tail": "lower_order",
    "powerplay": "powerplay_bowler", "pp": "powerplay_bowler", "new ball": "powerplay_bowler",
    "middle overs": "middle_overs_bowler", "middle_overs": "middle_overs_bowler",
    "death": "death_bowler", "death overs": "death_bowler",
    "pace": "pacer", "fast": "pacer", "spin": "spinner",
}
PHASES = ("powerplay", "middle", "death")
DEFAULT_TEMPLATE = {"opener": 2, "middle_order": 3, "finisher": 1,
                    "powerplay_bowler": 2, "middle_overs_bowler": 2, "death_bowler": 2}


def norm_role(role: str | None) -> str | None:
    if not role:
        return None
    r = role.strip().lower().replace("-", "_")
    r = ROLE_ALIASES.get(r, ROLE_ALIASES.get(r.replace("_", " "), r))
    return r


# ------------------------------------------------------------------ filters
@dataclass
class Filters:
    season_from: int | None = None
    season_to: int | None = None
    last_n_seasons: int | None = None
    venue: str | None = None
    team: str | None = None          # the player's own team
    opposition: str | None = None
    innings: int | None = None       # 1 = batting first, 2 = chasing
    phase: str | None = None         # powerplay | middle | death
    bowler_kind: str | None = None   # pace | spin   (batting queries)
    bowler_style: str | None = None  # RAP/LAP/OB/LB/SLA/LCH
    batter_hand: str | None = None   # L | R         (bowling queries)
    pos_min: int | None = None
    pos_max: int | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Filters":
        d = {k: v for k, v in (d or {}).items() if v not in (None, "", [])}
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"Unknown filter(s): {', '.join(sorted(unknown))}")
        for k in ("season_from", "season_to", "last_n_seasons", "innings", "pos_min", "pos_max"):
            if k in d:
                d[k] = int(d[k])
        if d.get("phase"):
            d["phase"] = str(d["phase"]).lower()
            if d["phase"] not in PHASES:
                raise ValueError(f"phase must be one of {PHASES}")
        if d.get("bowler_kind"):
            d["bowler_kind"] = str(d["bowler_kind"]).lower()
            if d["bowler_kind"] not in ("pace", "spin"):
                raise ValueError("bowler_kind must be 'pace' or 'spin'")
        if d.get("batter_hand"):
            d["batter_hand"] = str(d["batter_hand"]).upper()[:1]
            if d["batter_hand"] not in ("L", "R"):
                raise ValueError("batter_hand must be 'L' or 'R'")
        if d.get("innings") not in (None, 1, 2):
            raise ValueError("innings must be 1 or 2")
        if d.get("team"):
            d["team"] = canon_team(d["team"])
        if d.get("opposition"):
            d["opposition"] = canon_team(d["opposition"])
        if d.get("venue"):
            d["venue"] = canon_venue(d["venue"])
        return cls(**d)

    def describe(self) -> str:
        bits = []
        if self.last_n_seasons:
            bits.append(f"last {self.last_n_seasons} seasons" + (f" up to {self.season_to}" if self.season_to else ""))
        elif self.season_from or self.season_to:
            bits.append(f"seasons {self.season_from or 'start'}-{self.season_to or 'latest'}")
        if self.phase:
            bits.append(f"{self.phase} overs")
        if self.venue:
            bits.append(f"at {self.venue}")
        if self.team:
            bits.append(f"for {self.team}")
        if self.opposition:
            bits.append(f"vs {self.opposition}")
        if self.innings:
            bits.append("batting first" if self.innings == 1 else "chasing")
        if self.bowler_kind:
            bits.append(f"vs {self.bowler_kind}")
        if self.bowler_style:
            bits.append(f"vs {self.bowler_style}")
        if self.batter_hand:
            bits.append(f"vs {'left' if self.batter_hand == 'L' else 'right'}-handed batters")
        if self.pos_min or self.pos_max:
            bits.append(f"batting position {self.pos_min or 1}-{self.pos_max or 11}")
        return ", ".join(bits) or "all IPL data"


class _Q:
    """Tiny helper accumulating SQL parameters by name."""

    def __init__(self) -> None:
        self.params: dict = {}

    def p(self, v) -> str:
        k = f"p{len(self.params)}"
        self.params[k] = v
        return "$" + k


def _lit(names: list[str]) -> str:
    return "[" + ",".join("'" + n.replace("'", "''") + "'" for n in names) + "]"


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else round(v, 3)
    if hasattr(v, "item"):
        return _clean(v.item())
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def records(df: pd.DataFrame) -> list[dict]:
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]


# ------------------------------------------------------------------ engine
class Engine:
    def __init__(self, db_path: Path | str = config.DB_PATH):
        if not Path(db_path).exists():
            raise FileNotFoundError(f"{db_path} not found. Run: python -m datalens.loader")
        self.con = duckdb.connect(str(db_path), read_only=True,
                                  config={"enable_external_access": False})
        self.latest_season = self.con.execute("SELECT max(season) FROM balls").fetchone()[0]
        self.first_season = self.con.execute("SELECT min(season) FROM balls").fetchone()[0]
        self.players = [r[0] for r in self.con.execute(
            "SELECT DISTINCT p FROM (SELECT striker p FROM balls UNION SELECT bowler FROM balls) ORDER BY 1").fetchall()]
        w = self.con.execute("""SELECT p, count(*) FROM (SELECT striker p FROM balls WHERE season >= 2018
                                UNION ALL SELECT bowler FROM balls WHERE season >= 2018) GROUP BY p""").fetchall()
        self.resolver = PlayerResolver(self.players, {p: n for p, n in w})
        self._player_set = set(self.players)

    # ---- low-level -------------------------------------------------
    def query(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        cur = self.con.cursor()
        used = set(re.findall(r"\$(\w+)", sql))
        params = {k: v for k, v in (params or {}).items() if k in used}
        try:
            return cur.execute(sql, params).df()
        finally:
            cur.close()

    def _seasons(self, f: Filters) -> tuple[int, int]:
        to = f.season_to or self.latest_season
        frm = f.season_from or self.first_season
        if f.last_n_seasons:
            frm = max(frm, to - f.last_n_seasons + 1)
        return frm, to

    def resolve_player(self, name: str) -> str:
        if name in self._player_set:
            return name
        hit = self.resolver.resolve(name, threshold=0.75)
        if not hit:
            sugg = [p for p, _ in self.resolver.search(name, 3)]
            raise ValueError(f"Player '{name}' not found." + (f" Did you mean: {', '.join(sugg)}?" if sugg else ""))
        return hit

    # ---- metadata ----------------------------------------------------
    def meta(self) -> dict:
        teams = [r[0] for r in self.query(
            "SELECT DISTINCT batting_team FROM balls ORDER BY 1").values]
        venues = [r[0] for r in self.query(
            "SELECT venue FROM matches GROUP BY venue HAVING count(*) >= 3 ORDER BY count(*) DESC").values]
        return {
            "seasons": [self.first_season, self.latest_season], "teams": teams, "venues": venues,
            "bat_metrics": BAT_METRICS, "bowl_metrics": BOWL_METRICS,
            "bat_roles": BAT_ROLES, "bowl_roles": BOWL_ROLES, "phases": list(PHASES),
            "formula_functions": {"z(x)": "z-score of x across the ranked players",
                                  "pctile(x)": "percentile rank (0-100) of x",
                                  "abs, sqrt, log, min, max, round": "standard maths"},
            "counts": {
                "matches": int(self.query("SELECT count(*) c FROM matches")["c"][0]),
                "balls": int(self.query("SELECT count(*) c FROM balls")["c"][0]),
                "players": len(self.players),
            },
        }

    def active_players(self, since: int | None = None) -> set[str]:
        """Players who batted or bowled in season >= since (default: the latest season in the data)."""
        since = since or self.latest_season
        df = self.query(f"""SELECT striker AS p FROM balls WHERE season >= {int(since)}
                            UNION SELECT bowler FROM balls WHERE season >= {int(since)}""")
        return set(df["p"])

    def search_players(self, q: str, limit: int = 8) -> list[dict]:
        return [{"player": p, "score": round(s, 2)} for p, s in self.resolver.search(q, limit)]

    # ---- role tables -------------------------------------------------
    def batting_roles(self, f: Filters | None = None, min_innings: int = 8, role_min_share: float = 0.6,
                      finisher_death_share: float = 0.30) -> pd.DataFrame:
        f = f or Filters()
        q = _Q()
        frm, to = self._seasons(f)
        sql = f"""
        WITH inn AS (
          SELECT striker AS player, match_id, innings, min(striker_pos) AS pos,
                 sum(batter_faced::INT) AS faced,
                 sum((batter_faced AND over >= 15)::INT) AS faced_death,
                 sum((batter_faced AND over < 6)::INT) AS faced_pp
          FROM balls WHERE season BETWEEN {q.p(frm)} AND {q.p(to)} GROUP BY ALL)
        SELECT player, count(*) AS role_innings, sum(faced) AS role_balls,
               avg((pos <= 2)::INT) AS share_opener, avg((pos <= 3)::INT) AS share_top_order,
               avg((pos BETWEEN 4 AND 6)::INT) AS share_middle_order,
               avg((pos BETWEEN 4 AND 7)::INT) AS share_4_7, avg((pos >= 7)::INT) AS share_lower_order,
               sum(faced_death) * 1.0 / NULLIF(sum(faced), 0) AS death_share,
               sum(faced_pp) * 1.0 / NULLIF(sum(faced), 0) AS pp_share,
               round(avg(pos), 1) AS avg_pos, mode(pos) AS typical_pos
        FROM inn GROUP BY player HAVING count(*) >= {q.p(min_innings)}"""
        df = self.query(sql, q.params)
        t = role_min_share
        df["role_opener"] = df.share_opener >= t
        df["role_top_order"] = df.share_top_order >= t
        df["role_middle_order"] = df.share_middle_order >= t
        df["role_finisher"] = (df.share_4_7 >= t) & (df.death_share >= finisher_death_share)
        df["role_lower_order"] = df.share_lower_order >= t
        df["roles"] = df.apply(lambda r: [k for k in BAT_ROLES if r[f"role_{k}"]], axis=1)
        return df

    def bowling_roles(self, f: Filters | None = None, min_balls: int = 60, role_min_share: float | None = None) -> pd.DataFrame:
        f = f or Filters()
        q = _Q()
        frm, to = self._seasons(f)
        sql = f"""
        SELECT b.bowler AS player, sum(legal_ball::INT) AS role_balls,
               sum((legal_ball AND phase = 'powerplay')::INT) * 1.0 / sum(legal_ball::INT) AS pp_share,
               sum((legal_ball AND phase = 'middle')::INT) * 1.0 / sum(legal_ball::INT) AS middle_share,
               sum((legal_ball AND phase = 'death')::INT) * 1.0 / sum(legal_ball::INT) AS death_share,
               pm.bowl_kind, pm.bowl_style
        FROM balls b LEFT JOIN players_meta pm ON pm.player = b.bowler
        WHERE b.season BETWEEN {q.p(frm)} AND {q.p(to)}
        GROUP BY b.bowler, pm.bowl_kind, pm.bowl_style
        HAVING sum(legal_ball::INT) >= {q.p(min_balls)}"""
        df = self.query(sql, q.params)
        t_pp = role_min_share or 0.4
        t_mid = role_min_share or 0.5
        t_death = role_min_share or 0.3
        df["role_powerplay_bowler"] = df.pp_share >= t_pp
        df["role_middle_overs_bowler"] = df.middle_share >= t_mid
        df["role_death_bowler"] = df.death_share >= t_death
        df["role_pacer"] = df.bowl_kind == "pace"
        df["role_spinner"] = df.bowl_kind == "spin"
        df["roles"] = df.apply(lambda r: [k for k in BOWL_ROLES if r[f"role_{k}"]], axis=1)
        return df

    # ---- ranking -----------------------------------------------------
    def _base_from(self, side: str, f: Filters, q: _Q) -> tuple[str, str, list[str]]:
        """Return (key column, FROM/JOIN clause, WHERE list) for the filtered ball set."""
        key = "b.striker" if side == "bat" else "b.bowler"
        joins = ["balls b"]
        where = []
        frm, to = self._seasons(f)
        where.append(f"b.season BETWEEN {q.p(frm)} AND {q.p(to)}")
        if f.venue:
            where.append(f"b.venue = {q.p(f.venue)}")
        if f.innings:
            where.append(f"b.innings = {q.p(f.innings)}")
        if f.phase:
            where.append(f"b.phase = {q.p(f.phase)}")
        own, opp = ("b.batting_team", "b.bowling_team") if side == "bat" else ("b.bowling_team", "b.batting_team")
        if f.team:
            where.append(f"{own} = {q.p(f.team)}")
        if f.opposition:
            where.append(f"{opp} = {q.p(f.opposition)}")
        if f.pos_min:
            where.append(f"b.striker_pos >= {q.p(f.pos_min)}")
        if f.pos_max:
            where.append(f"b.striker_pos <= {q.p(f.pos_max)}")
        if f.bowler_kind or f.bowler_style:
            joins.append("JOIN players_meta pbw ON pbw.player = b.bowler")
            if f.bowler_kind:
                where.append(f"pbw.bowl_kind = {q.p(f.bowler_kind)}")
            if f.bowler_style:
                where.append(f"pbw.bowl_style = {q.p(f.bowler_style.upper())}")
        if f.batter_hand:
            joins.append("JOIN players_meta pbt ON pbt.player = b.striker")
            where.append(f"pbt.bat_hand = {q.p(f.batter_hand)}")
        return key, " ".join(joins), where

    def rank(self, side: str, *, filters: dict | Filters | None = None, formula: str | None = None,
             role: str | None = None, role_min_share: float | None = None, min_balls: int | None = None,
             limit: int = 25, order: str | None = None, min_role_innings: int = 8,
             players: list[str] | None = None, role_filters: Filters | None = None) -> dict:
        """Rank batters (side='bat') or bowlers (side='bowl') by a metric or custom formula."""
        if side not in ("bat", "bowl"):
            raise ValueError("side must be 'bat' or 'bowl'")
        f = filters if isinstance(filters, Filters) else Filters.from_dict(filters)
        metrics = BAT_METRICS if side == "bat" else BOWL_METRICS
        formula = (formula or ("strike_rate" if side == "bat" else "economy")).strip()
        try:
            score_sql = compile_formula(formula, set(metrics))
        except FormulaError as e:
            raise ValueError(str(e)) from None
        if order is None:
            order = "asc" if formula in LOWER_IS_BETTER else "desc"
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        limit = max(1, min(int(limit), 200))
        if min_balls is None:
            restricted = bool(f.phase or f.bowler_kind or f.bowler_style or f.venue or f.opposition
                              or f.batter_hand or f.team or f.innings or f.pos_min or f.pos_max)
            min_balls = (60 if restricted else 150) if side == "bat" else (60 if restricted else 120)

        warnings: list[str] = []
        role = norm_role(role)
        role_tags_info = None
        allowed: list[str] | None = None
        if role:
            rf = role_filters or Filters(season_from=f.season_from, season_to=f.season_to, last_n_seasons=f.last_n_seasons)
            if side == "bat":
                if role not in BAT_ROLES:
                    raise ValueError(f"Unknown batting role '{role}'. Options: {', '.join(BAT_ROLES)}")
                rt = self.batting_roles(rf, min_innings=min_role_innings, role_min_share=role_min_share or 0.6)
            else:
                if role not in BOWL_ROLES:
                    raise ValueError(f"Unknown bowling role '{role}'. Options: {', '.join(BOWL_ROLES)}")
                rt = self.bowling_roles(rf, role_min_share=role_min_share)
            allowed = rt.loc[rt[f"role_{role}"], "player"].tolist()
            role_tags_info = {"role": role, "definition": (BAT_ROLES | BOWL_ROLES)[role], "qualifying_players": len(allowed)}
            if not allowed:
                return self._empty(side, f, formula, role_tags_info, ["No players match that role in the chosen seasons."])
        if players:
            names = [self.resolve_player(p) for p in players]
            allowed = names if allowed is None else [p for p in allowed if p in set(names)]
            if not allowed:
                return self._empty(side, f, formula, role_tags_info, ["None of the given players match."])

        q = _Q()
        key, from_sql, where = self._base_from(side, f, q)
        if allowed is not None:
            where.append(f"{key} IN (SELECT unnest({_lit(allowed)}))")
        where_sql = " AND ".join(where)
        if side == "bat":
            agg = f"""SELECT {key} AS player,
                 count(DISTINCT b.match_id * 10 + b.innings) AS innings,
                 sum(b.runs_off_bat) AS runs, sum(b.batter_faced::INT) AS balls,
                 sum(b.striker_out::INT) AS dismissals,
                 sum((b.runs_off_bat = 4 AND b.is_boundary)::INT) AS fours,
                 sum((b.runs_off_bat = 6 AND b.is_boundary)::INT) AS sixes,
                 sum((b.batter_faced AND b.runs_off_bat = 0)::INT) AS dots
               FROM {from_sql} WHERE {where_sql} GROUP BY {key}"""
            derived = """SELECT *, runs * 100.0 / NULLIF(balls, 0) AS strike_rate,
                 runs * 1.0 / NULLIF(dismissals, 0) AS average,
                 (fours + sixes) * 100.0 / NULLIF(balls, 0) AS boundary_pct,
                 dots * 100.0 / NULLIF(balls, 0) AS dot_pct,
                 balls * 1.0 / NULLIF(dismissals, 0) AS balls_per_dismissal,
                 runs * 1.0 / NULLIF(innings, 0) AS runs_per_innings,
                 (fours * 4 + sixes * 6) * 100.0 / NULLIF(runs, 0) AS boundary_runs_pct FROM agg"""
        else:
            agg = f"""SELECT {key} AS player,
                 count(DISTINCT b.match_id * 10 + b.innings) AS innings,
                 sum(b.legal_ball::INT) AS balls, sum(b.runs_conceded) AS runs,
                 sum(b.bowler_wicket::INT) AS wickets,
                 sum((b.legal_ball AND b.runs_conceded = 0)::INT) AS dots,
                 sum((b.runs_off_bat = 4 AND b.is_boundary)::INT) AS fours,
                 sum((b.runs_off_bat = 6 AND b.is_boundary)::INT) AS sixes,
                 sum((b.wides > 0)::INT) AS wides, sum((b.noballs > 0)::INT) AS noballs
               FROM {from_sql} WHERE {where_sql} GROUP BY {key}"""
            derived = """SELECT *, balls / 6.0 AS overs, runs * 6.0 / NULLIF(balls, 0) AS economy,
                 runs * 1.0 / NULLIF(wickets, 0) AS bowling_average,
                 balls * 1.0 / NULLIF(wickets, 0) AS bowling_sr,
                 dots * 100.0 / NULLIF(balls, 0) AS dot_pct,
                 (fours + sixes) * 100.0 / NULLIF(balls, 0) AS boundary_pct,
                 wickets * 6.0 / NULLIF(balls, 0) AS wickets_per_over FROM agg"""
        sql = f"""WITH agg AS ({agg}), stats AS ({derived}),
            filtered AS (SELECT * FROM stats WHERE balls >= {q.p(min_balls)}),
            scored AS (SELECT *, {score_sql} AS score FROM filtered)
          SELECT s.*, pm.nationality, pm.bat_hand, pm.bowl_style
          FROM scored s LEFT JOIN players_meta pm ON pm.player = s.player
          WHERE s.score IS NOT NULL
          ORDER BY s.score {order.upper()} NULLS LAST, s.balls DESC LIMIT {q.p(limit)}"""
        df = self.query(sql, q.params)
        df.insert(0, "rank", range(1, len(df) + 1))
        if len(df):
            small = df["balls"] < 1.5 * min_balls
            df["small_sample"] = small
            if small.any():
                warnings.append(f"{int(small.sum())} player(s) are close to the minimum sample ({min_balls} balls) - treat with care.")
        if f.bowler_kind or f.bowler_style or f.batter_hand:
            warnings.append(self._coverage_note(f))
        return {
            "side": side, "formula": formula, "order": order, "min_balls": min_balls,
            "filters": asdict(f), "filters_text": f.describe(), "role": role_tags_info,
            "columns": list(df.columns), "rows": records(df), "warnings": [w for w in warnings if w],
            "sql": sql, "params": q.params,
        }

    def _empty(self, side, f, formula, role_info, warnings) -> dict:
        return {"side": side, "formula": formula, "filters": asdict(f), "filters_text": f.describe(),
                "role": role_info, "columns": [], "rows": [], "warnings": warnings, "sql": "", "params": {}}

    def _coverage_note(self, f: Filters) -> str:
        q = _Q()
        frm, to = self._seasons(f)
        df = self.query(f"""SELECT avg(CASE WHEN pm.bowl_kind IS NOT NULL THEN 1 ELSE 0 END) AS bowl_known,
                                   avg(CASE WHEN pt.bat_hand IS NOT NULL THEN 1 ELSE 0 END) AS bat_known
            FROM balls b LEFT JOIN players_meta pm ON pm.player = b.bowler
                         LEFT JOIN players_meta pt ON pt.player = b.striker
            WHERE b.season BETWEEN {q.p(frm)} AND {q.p(to)}""", q.params)
        if f.batter_hand:
            return f"Batting-hand data covers {df['bat_known'][0] * 100:.0f}% of deliveries in this window; others are excluded."
        return f"Bowling-style data covers {df['bowl_known'][0] * 100:.0f}% of deliveries in this window; others are excluded."

    # ---- matchups ----------------------------------------------------
    def batter_vs_bowler_types(self, batter: str, filters: dict | None = None) -> dict:
        batter = self.resolve_player(batter)
        f = Filters.from_dict(filters)
        q = _Q()
        frm, to = self._seasons(f)
        extra = f" AND b.venue = {q.p(f.venue)}" if f.venue else ""
        sql = f"""
        SELECT COALESCE(pm.bowl_kind, 'unknown') AS bowler_kind, COALESCE(pm.bowl_style, 'unknown') AS bowler_style,
               sum(b.runs_off_bat) AS runs, sum(b.batter_faced::INT) AS balls, sum(b.striker_out::INT) AS dismissals,
               sum((b.is_boundary)::INT) AS boundaries,
               sum((b.batter_faced AND b.runs_off_bat = 0)::INT) AS dots
        FROM balls b LEFT JOIN players_meta pm ON pm.player = b.bowler
        WHERE b.striker = {q.p(batter)} AND b.season BETWEEN {q.p(frm)} AND {q.p(to)}{extra}
        GROUP BY ALL HAVING sum(b.batter_faced::INT) > 0 ORDER BY balls DESC"""
        df = self.query(sql, q.params)
        df["strike_rate"] = df.runs * 100.0 / df.balls
        df["average"] = df.runs / df.dismissals.where(df.dismissals > 0)
        df["boundary_pct"] = df.boundaries * 100.0 / df.balls
        df["dot_pct"] = df.dots * 100.0 / df.balls
        kinds = df.groupby("bowler_kind", as_index=False)[["runs", "balls", "dismissals", "boundaries", "dots"]].sum()
        kinds["strike_rate"] = kinds.runs * 100.0 / kinds.balls
        kinds["average"] = kinds.runs / kinds.dismissals.where(kinds.dismissals > 0)
        kinds["dot_pct"] = kinds.dots * 100.0 / kinds.balls
        kinds["boundary_pct"] = kinds.boundaries * 100.0 / kinds.balls
        total = int(df.balls.sum())
        unknown = int(df.loc[df.bowler_kind == "unknown", "balls"].sum())
        warn = []
        if total and unknown / total > 0.15:
            warn.append(f"{unknown / total * 100:.0f}% of balls faced were against bowlers without style data; they appear as 'unknown'.")
        return {"player": batter, "filters_text": f.describe(), "by_kind": records(kinds), "by_style": records(df), "warnings": warn}

    def bowler_vs_batter_hands(self, bowler: str, filters: dict | None = None) -> dict:
        bowler = self.resolve_player(bowler)
        f = Filters.from_dict(filters)
        q = _Q()
        frm, to = self._seasons(f)
        sql = f"""
        SELECT COALESCE(pm.bat_hand, 'unknown') AS batter_hand,
               sum(b.legal_ball::INT) AS balls, sum(b.runs_conceded) AS runs, sum(b.bowler_wicket::INT) AS wickets,
               sum((b.legal_ball AND b.runs_conceded = 0)::INT) AS dots, sum(b.is_boundary::INT) AS boundaries
        FROM balls b LEFT JOIN players_meta pm ON pm.player = b.striker
        WHERE b.bowler = {q.p(bowler)} AND b.season BETWEEN {q.p(frm)} AND {q.p(to)}
        GROUP BY ALL HAVING sum(b.legal_ball::INT) > 0 ORDER BY balls DESC"""
        df = self.query(sql, q.params)
        df["economy"] = df.runs * 6.0 / df.balls
        df["bowling_average"] = df.runs / df.wickets.where(df.wickets > 0)
        df["dot_pct"] = df.dots * 100.0 / df.balls
        df["boundary_pct"] = df.boundaries * 100.0 / df.balls
        return {"player": bowler, "filters_text": f.describe(), "by_hand": records(df), "warnings": []}

    def head_to_head(self, batter: str, bowler: str, filters: dict | None = None) -> dict:
        batter, bowler = self.resolve_player(batter), self.resolve_player(bowler)
        f = Filters.from_dict(filters)
        q = _Q()
        frm, to = self._seasons(f)
        sql = f"""
        SELECT b.season, sum(b.runs_off_bat) AS runs, sum(b.batter_faced::INT) AS balls,
               sum(b.striker_out::INT) AS dismissals, sum(b.is_boundary::INT) AS boundaries,
               sum((b.batter_faced AND b.runs_off_bat = 0)::INT) AS dots
        FROM balls b WHERE b.striker = {q.p(batter)} AND b.bowler = {q.p(bowler)}
          AND b.season BETWEEN {q.p(frm)} AND {q.p(to)} GROUP BY b.season ORDER BY b.season"""
        df = self.query(sql, q.params)
        tot = {"runs": int(df.runs.sum()), "balls": int(df.balls.sum()), "dismissals": int(df.dismissals.sum()),
               "boundaries": int(df.boundaries.sum()), "dots": int(df.dots.sum())} if len(df) else \
            {"runs": 0, "balls": 0, "dismissals": 0, "boundaries": 0, "dots": 0}
        tot["strike_rate"] = tot["runs"] * 100.0 / tot["balls"] if tot["balls"] else None
        tot["average"] = tot["runs"] / tot["dismissals"] if tot["dismissals"] else None
        warn = []
        if tot["balls"] < 30:
            warn.append(f"Only {tot['balls']} balls in this matchup - too small to draw firm conclusions.")
        return {"batter": batter, "bowler": bowler, "filters_text": f.describe(), "total": _rec(tot),
                "by_season": records(df), "warnings": warn}

    def opponents(self, player: str, side: str = "bat", min_balls: int = 12, limit: int = 15,
                  filters: dict | None = None) -> dict:
        """Best and worst matchups for a player against individual opponents."""
        player = self.resolve_player(player)
        f = Filters.from_dict(filters)
        q = _Q()
        frm, to = self._seasons(f)
        if side == "bat":
            sql = f"""SELECT bowler AS opponent, sum(runs_off_bat) AS runs, sum(batter_faced::INT) AS balls,
                      sum(striker_out::INT) AS dismissals, sum(is_boundary::INT) AS boundaries
                FROM balls WHERE striker = {q.p(player)} AND season BETWEEN {q.p(frm)} AND {q.p(to)}
                GROUP BY 1 HAVING sum(batter_faced::INT) >= {q.p(min_balls)}"""
            df = self.query(sql, q.params)
            df["strike_rate"] = df.runs * 100.0 / df.balls
            df["average"] = df.runs / df.dismissals.where(df.dismissals > 0)
            best = df.sort_values("strike_rate", ascending=False).head(limit)
            worst = df.sort_values(["dismissals", "strike_rate"], ascending=[False, True]).head(limit)
        else:
            sql = f"""SELECT striker AS opponent, sum(runs_conceded) AS runs, sum(legal_ball::INT) AS balls,
                      sum(bowler_wicket::INT) AS wickets, sum(is_boundary::INT) AS boundaries
                FROM balls WHERE bowler = {q.p(player)} AND season BETWEEN {q.p(frm)} AND {q.p(to)}
                GROUP BY 1 HAVING sum(legal_ball::INT) >= {q.p(min_balls)}"""
            df = self.query(sql, q.params)
            df["economy"] = df.runs * 6.0 / df.balls
            best = df.sort_values(["wickets", "economy"], ascending=[False, True]).head(limit)
            worst = df.sort_values("economy", ascending=False).head(limit)
        return {"player": player, "side": side, "best": records(best), "worst": records(worst),
                "filters_text": f.describe(), "warnings": []}

    # ---- venues ------------------------------------------------------
    def venue_profile(self, venue: str | None = None, filters: dict | None = None, min_matches: int = 3) -> dict:
        f = Filters.from_dict(filters)
        if venue:
            f.venue = canon_venue(venue)
        q = _Q()
        frm, to = self._seasons(f)
        vf = f"AND m.venue = {q.p(f.venue)}" if f.venue else ""
        sql = f"""
        WITH mm AS (
          SELECT venue, count(*) AS matches, avg(inn1_runs) AS avg_first_innings, avg(inn2_runs) AS avg_second_innings,
                 avg(CASE WHEN winner IS NOT NULL THEN (winner = first_bat_team)::INT END) * 100 AS bat_first_win_pct,
                 avg(CASE WHEN winner IS NOT NULL THEN (winner = toss_winner)::INT END) * 100 AS toss_winner_win_pct,
                 avg((toss_decision = 'field')::INT) * 100 AS chose_to_field_pct
          FROM matches m WHERE season BETWEEN {q.p(frm)} AND {q.p(to)} {vf} GROUP BY venue HAVING count(*) >= {q.p(min_matches)}),
        ph AS (
          SELECT venue,
            sum(CASE WHEN phase='powerplay' THEN runs_off_bat+extras END) * 6.0 / NULLIF(sum((phase='powerplay' AND legal_ball)::INT),0) AS powerplay_rpo,
            sum(CASE WHEN phase='middle' THEN runs_off_bat+extras END) * 6.0 / NULLIF(sum((phase='middle' AND legal_ball)::INT),0) AS middle_rpo,
            sum(CASE WHEN phase='death' THEN runs_off_bat+extras END) * 6.0 / NULLIF(sum((phase='death' AND legal_ball)::INT),0) AS death_rpo,
            sum(is_boundary::INT) * 100.0 / NULLIF(sum(batter_faced::INT),0) AS boundary_pct,
            sum(bowler_wicket::INT) * 1.0 / count(DISTINCT match_id) AS wickets_per_match
          FROM balls b WHERE season BETWEEN {q.p(frm)} AND {q.p(to)} GROUP BY venue)
        SELECT mm.*, ph.powerplay_rpo, ph.middle_rpo, ph.death_rpo, ph.boundary_pct, ph.wickets_per_match
        FROM mm JOIN ph USING (venue) ORDER BY matches DESC"""
        df = self.query(sql, q.params)
        notes = []
        if not len(df):
            notes.append("No venue with enough matches for these filters.")
        return {"filters_text": f.describe(), "venues": records(df), "warnings": notes}

    # ---- player profile ----------------------------------------------
    def player_profile(self, player: str, filters: dict | None = None) -> dict:
        player = self.resolve_player(player)
        f = Filters.from_dict(filters)
        frm, to = self._seasons(f)
        q = _Q()
        bat = self.query(f"""
          SELECT season, count(DISTINCT match_id*10+innings) AS innings, sum(runs_off_bat) AS runs,
                 sum(batter_faced::INT) AS balls, sum(striker_out::INT) AS dismissals,
                 sum(is_boundary::INT) AS boundaries, round(avg(striker_pos),1) AS avg_pos
          FROM balls WHERE striker = {q.p(player)} AND season BETWEEN {q.p(frm)} AND {q.p(to)}
          GROUP BY season ORDER BY season""", q.params)
        if len(bat):
            bat["strike_rate"] = bat.runs * 100.0 / bat.balls
            bat["average"] = bat.runs / bat.dismissals.where(bat.dismissals > 0)
        bph = self.query(f"""
          SELECT phase, sum(runs_off_bat) AS runs, sum(batter_faced::INT) AS balls, sum(striker_out::INT) AS dismissals
          FROM balls WHERE striker = {q.p(player)} AND season BETWEEN {q.p(frm)} AND {q.p(to)} GROUP BY phase""", q.params)
        if len(bph):
            bph["strike_rate"] = bph.runs * 100.0 / bph.balls
        pos = self.query(f"""
          SELECT pos, count(*) AS innings FROM (SELECT min(striker_pos) AS pos FROM balls
            WHERE striker = {q.p(player)} AND season BETWEEN {q.p(frm)} AND {q.p(to)} GROUP BY match_id, innings)
          GROUP BY pos ORDER BY pos""", q.params)
        bowl = self.query(f"""
          SELECT season, count(DISTINCT match_id*10+innings) AS innings, sum(legal_ball::INT) AS balls,
                 sum(runs_conceded) AS runs, sum(bowler_wicket::INT) AS wickets
          FROM balls WHERE bowler = {q.p(player)} AND season BETWEEN {q.p(frm)} AND {q.p(to)}
          GROUP BY season ORDER BY season""", q.params)
        if len(bowl):
            bowl["economy"] = bowl.runs * 6.0 / bowl.balls
        wph = self.query(f"""
          SELECT phase, sum(legal_ball::INT) AS balls, sum(runs_conceded) AS runs, sum(bowler_wicket::INT) AS wickets
          FROM balls WHERE bowler = {q.p(player)} AND season BETWEEN {q.p(frm)} AND {q.p(to)} GROUP BY phase""", q.params)
        if len(wph):
            wph["economy"] = wph.runs * 6.0 / wph.balls
        rf = Filters(season_from=frm, season_to=to)
        bt = self.batting_roles(rf, min_innings=3)
        wt = self.bowling_roles(rf, min_balls=30)
        roles = (bt.loc[bt.player == player, "roles"].tolist() or [[]])[0] + \
                (wt.loc[wt.player == player, "roles"].tolist() or [[]])[0]
        meta = self.query(f"SELECT nationality, bat_hand, bowl_style FROM players_meta WHERE player = {q.p(player)}", q.params)
        return {"player": player, "filters_text": f.describe(), "roles": roles,
                "meta": records(meta)[0] if len(meta) else {},
                "batting_by_season": records(bat), "batting_by_phase": records(bph),
                "batting_positions": records(pos), "bowling_by_season": records(bowl),
                "bowling_by_phase": records(wph), "warnings": []}

    # ---- squad gap analysis ------------------------------------------
    def squad_gaps(self, squad: list[str], template: dict[str, int] | None = None,
                   filters: dict | None = None) -> dict:
        f = Filters.from_dict(filters)
        if not f.last_n_seasons and not f.season_from:
            f.last_n_seasons = 3
        template = {norm_role(k): int(v) for k, v in (template or DEFAULT_TEMPLATE).items()}
        resolved, unresolved = [], []
        for name in squad:
            hit = name if name in self._player_set else self.resolver.resolve(name, 0.8)
            (resolved if hit else unresolved).append(hit or name)
        bt = self.batting_roles(f, min_innings=3)
        wt = self.bowling_roles(f, min_balls=30)
        rows, counts = [], {r: 0 for r in template}
        for p in resolved:
            tags = (bt.loc[bt.player == p, "roles"].tolist() or [[]])[0] + \
                   (wt.loc[wt.player == p, "roles"].tolist() or [[]])[0]
            for t in tags:
                if t in counts:
                    counts[t] += 1
            rows.append({"player": p, "roles": tags})
        gaps = {r: max(0, need - counts[r]) for r, need in template.items()}
        return {"squad": rows, "unresolved": unresolved, "role_counts": counts, "template": template,
                "gaps": {r: g for r, g in gaps.items() if g > 0}, "filters_text": f.describe(),
                "warnings": ["Roles are inferred from recent IPL data; players with few appearances may have no role tag."]}


def _rec(d: dict) -> dict:
    return {k: _clean(v) for k, v in d.items()}
