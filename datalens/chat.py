"""Natural-language interface.

Two modes share the same tool layer so every answer can be traced to a concrete query:

* **Claude mode** (needs ANTHROPIC_API_KEY): the model decides which analytics tools to call.
* **Rules mode** (no key): a deterministic parser maps common questions onto the same tools.
"""
from __future__ import annotations

import json
import os
import re

from . import similarity
from .engine import BAT_ROLES, BOWL_ROLES, Engine, norm_role
from .names import VENUE_RULES, canon_venue

MODEL = os.environ.get("DATALENS_MODEL", "claude-sonnet-5-5")
MAX_SQL_ROWS = 200

BLOCKED_SQL = re.compile(
    r"\b(attach|detach|copy|pragma|install|load|export|import|create|drop|insert|update|delete|alter|call|set|"
    r"read_\w+|glob|parquet_scan|csv_scan|json_scan|httpfs|truncate|replace|vacuum|checkpoint)\b", re.I)


def safe_select(sql: str) -> str:
    """Validate a user/model supplied SQL string: one read-only SELECT, nothing else."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise ValueError("Empty SQL")
    if ";" in s:
        raise ValueError("Only a single statement is allowed")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise ValueError("Only SELECT queries are allowed")
    if BLOCKED_SQL.search(s):
        raise ValueError("That SQL uses a keyword that is not allowed (read-only queries only)")
    return f"SELECT * FROM ({s}) AS q LIMIT {MAX_SQL_ROWS}"


SCHEMA_HELP = """Tables (IPL ball-by-ball, Cricsheet, one row per delivery, super overs excluded):
balls(match_id, season, match_date, venue, innings, over[0-19], phase['powerplay'|'middle'|'death'],
  batting_team, bowling_team, striker, non_striker, bowler, runs_off_bat, extras, wides, noballs, byes, legbyes,
  wicket_type, player_dismissed, batter_faced[bool], legal_ball[bool], runs_conceded, bowler_wicket[bool],
  striker_out[bool], is_boundary[bool], striker_pos[1-11 batting position])
matches(match_id, season, match_date, venue, team1, team2, toss_winner, toss_decision, winner, player_of_match,
  first_bat_team, inn1_runs, inn2_runs)
players_meta(player, nationality['IND'|'OS'], bat_hand['L'|'R'], bowl_style['RAP','LAP','OB','LB','SLA','LCH'], bowl_kind['pace'|'spin'])
Player names use Cricsheet format, e.g. 'V Kohli', 'JJ Bumrah', 'RG Sharma', 'MS Dhoni'."""

SYSTEM_PROMPT = f"""You are DataLens AI, a cricket analytics assistant for IPL data.
Always answer by calling tools - never invent statistics. Prefer rank_players for rankings (it understands roles,
phases and custom formulas); use run_sql only for questions the other tools cannot answer.
Roles are inferred from data: batting roles from batting position (opener 1-2, top_order 1-3, middle_order 4-6,
finisher = 4-7 with many balls in overs 16-20); bowling roles from the share of balls bowled per phase.
Always mention sample sizes, point out small samples, and state which filters you applied.
Be concise. Format numbers sensibly (strike rate 1 decimal, economy 2 decimals).

{SCHEMA_HELP}"""

FILTER_SCHEMA = {
    "type": "object",
    "description": "Optional filters",
    "properties": {
        "season_from": {"type": "integer"}, "season_to": {"type": "integer"},
        "last_n_seasons": {"type": "integer", "description": "Only the most recent N seasons"},
        "venue": {"type": "string"}, "team": {"type": "string", "description": "the player's own team"},
        "opposition": {"type": "string"}, "innings": {"type": "integer", "enum": [1, 2]},
        "phase": {"type": "string", "enum": ["powerplay", "middle", "death"]},
        "bowler_kind": {"type": "string", "enum": ["pace", "spin"], "description": "batting queries: restrict to deliveries from this bowler type"},
        "batter_hand": {"type": "string", "enum": ["L", "R"], "description": "bowling queries: restrict to this batter hand"},
        "pos_min": {"type": "integer"}, "pos_max": {"type": "integer"},
    },
}

TOOLS = [
    {"name": "rank_players",
     "description": "Rank batters or bowlers by a metric or custom formula, optionally restricted to a role "
                    "(e.g. middle_order, opener, finisher, death_bowler, powerplay_bowler, spinner) and filters.",
     "input_schema": {"type": "object", "properties": {
         "side": {"type": "string", "enum": ["bat", "bowl"]},
         "role": {"type": "string", "description": "one of: " + ", ".join(list(BAT_ROLES) + list(BOWL_ROLES))},
         "formula": {"type": "string", "description": "metric or expression, e.g. strike_rate or 0.5*z(strike_rate)+0.5*z(average). "
                     "Batting metrics: runs, balls, innings, dismissals, fours, sixes, strike_rate, average, boundary_pct, dot_pct, "
                     "balls_per_dismissal, runs_per_innings, boundary_runs_pct. Bowling: balls, overs, runs, wickets, economy, "
                     "bowling_average, bowling_sr, dot_pct, boundary_pct, wickets_per_over, wides, noballs. Functions: z(), pctile(), abs, sqrt, log, min, max."},
         "filters": FILTER_SCHEMA, "min_balls": {"type": "integer"}, "limit": {"type": "integer"},
         "order": {"type": "string", "enum": ["asc", "desc"]}}, "required": ["side"]}},
    {"name": "batter_vs_bowler_types",
     "description": "How a batter performs against pace vs spin (and bowling styles).",
     "input_schema": {"type": "object", "properties": {"batter": {"type": "string"}, "filters": FILTER_SCHEMA}, "required": ["batter"]}},
    {"name": "bowler_vs_batter_hands",
     "description": "How a bowler performs against left- vs right-handed batters.",
     "input_schema": {"type": "object", "properties": {"bowler": {"type": "string"}, "filters": FILTER_SCHEMA}, "required": ["bowler"]}},
    {"name": "head_to_head",
     "description": "Batter vs bowler record.",
     "input_schema": {"type": "object", "properties": {"batter": {"type": "string"}, "bowler": {"type": "string"}, "filters": FILTER_SCHEMA}, "required": ["batter", "bowler"]}},
    {"name": "venue_profile",
     "description": "Venue scoring, phase run rates, chase/toss tendencies. Omit venue for all venues.",
     "input_schema": {"type": "object", "properties": {"venue": {"type": "string"}, "filters": FILTER_SCHEMA}}},
    {"name": "player_profile",
     "description": "Season-by-season stats, phase split, batting positions and inferred roles for a player.",
     "input_schema": {"type": "object", "properties": {"player": {"type": "string"}, "filters": FILTER_SCHEMA}, "required": ["player"]}},
    {"name": "similar_players",
     "description": "Find players with a similar statistical profile (replacement finder).",
     "input_schema": {"type": "object", "properties": {"player": {"type": "string"}, "side": {"type": "string", "enum": ["bat", "bowl"]},
                                                       "n": {"type": "integer"}, "filters": FILTER_SCHEMA}, "required": ["player"]}},
    {"name": "run_sql",
     "description": "Run one read-only SELECT (DuckDB SQL) against the tables described in the system prompt. Max 200 rows.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


class ToolBox:
    def __init__(self, engine: Engine):
        self.e = engine

    def call(self, name: str, args: dict) -> dict:
        a = dict(args or {})
        if name == "rank_players":
            return self.e.rank(a.get("side", "bat"), filters=a.get("filters"), formula=a.get("formula"), role=a.get("role"),
                               min_balls=a.get("min_balls"), limit=a.get("limit", 10), order=a.get("order"))
        if name == "batter_vs_bowler_types":
            return self.e.batter_vs_bowler_types(a["batter"], a.get("filters"))
        if name == "bowler_vs_batter_hands":
            return self.e.bowler_vs_batter_hands(a["bowler"], a.get("filters"))
        if name == "head_to_head":
            return self.e.head_to_head(a["batter"], a["bowler"], a.get("filters"))
        if name == "venue_profile":
            return self.e.venue_profile(a.get("venue"), a.get("filters"))
        if name == "player_profile":
            return self.e.player_profile(a["player"], a.get("filters"))
        if name == "similar_players":
            return similarity.similar_players(self.e, a["player"], a.get("side", "bat"), n=a.get("n", 8), filters=a.get("filters"))
        if name == "run_sql":
            sql = safe_select(a["query"])
            df = self.e.query(sql)
            from .engine import records
            return {"columns": list(df.columns), "rows": records(df), "sql": a["query"], "warnings": []}
        raise ValueError(f"Unknown tool {name}")


# ------------------------------------------------------------------ table extraction for the UI
RANK_COLS_BAT = ["rank", "player", "innings", "balls", "runs", "strike_rate", "average", "boundary_pct", "dot_pct", "score"]
RANK_COLS_BOWL = ["rank", "player", "innings", "balls", "wickets", "economy", "bowling_average", "dot_pct", "score"]


def tables_from(name: str, res: dict) -> list[dict]:
    t: list[dict] = []
    if name == "rank_players":
        cols = RANK_COLS_BAT if res.get("side") == "bat" else RANK_COLS_BOWL
        cols = [c for c in cols if c in res.get("columns", [])]
        t.append({"title": f"{res['formula']} - {res['filters_text']}" + (f" - role: {res['role']['role']}" if res.get("role") else ""),
                  "columns": cols, "rows": [{c: r.get(c) for c in cols} for r in res["rows"]]})
    elif name == "batter_vs_bowler_types":
        t.append({"title": f"{res['player']} by bowler type", "columns": ["bowler_kind", "balls", "runs", "dismissals", "strike_rate", "average", "dot_pct", "boundary_pct"], "rows": res["by_kind"]})
    elif name == "bowler_vs_batter_hands":
        t.append({"title": f"{res['player']} by batter hand", "columns": ["batter_hand", "balls", "runs", "wickets", "economy", "dot_pct"], "rows": res["by_hand"]})
    elif name == "head_to_head":
        t.append({"title": f"{res['batter']} vs {res['bowler']}", "columns": ["season", "balls", "runs", "dismissals", "boundaries"], "rows": res["by_season"]})
    elif name == "venue_profile":
        cols = ["venue", "matches", "avg_first_innings", "avg_second_innings", "bat_first_win_pct", "powerplay_rpo", "middle_rpo", "death_rpo"]
        t.append({"title": "Venue profile", "columns": cols, "rows": res["venues"]})
    elif name == "player_profile":
        t.append({"title": f"{res['player']} - batting by season", "columns": ["season", "innings", "runs", "balls", "strike_rate", "average", "avg_pos"], "rows": res["batting_by_season"]})
        if res["bowling_by_season"]:
            t.append({"title": f"{res['player']} - bowling by season", "columns": ["season", "innings", "balls", "wickets", "economy"], "rows": res["bowling_by_season"]})
    elif name == "similar_players":
        t.append({"title": f"Players similar to {res['player']}", "columns": ["player", "similarity", "nationality", "closest_on"], "rows": res["rows"]})
    elif name == "run_sql":
        t.append({"title": "SQL result", "columns": res["columns"], "rows": res["rows"]})
    return t


def summarise(name: str, res: dict) -> str:
    n = len(res.get("rows", []))
    if name == "rank_players":
        top = ", ".join(f"{r['player']}" for r in res["rows"][:3])
        return f"{n} players ranked; top: {top}" if n else "no players matched"
    return "ok"


# ------------------------------------------------------------------ Claude mode
def _shrink(res: dict, max_rows: int = 25) -> dict:
    out = {k: v for k, v in res.items() if k not in ("sql", "params", "columns")}
    for k, v in list(out.items()):
        if isinstance(v, list) and len(v) > max_rows:
            out[k] = v[:max_rows]
    return out


class ChatAgent:
    def __init__(self, engine: Engine, client=None):
        self.e = engine
        self.tools = ToolBox(engine)
        self.client = client
        if self.client is None and os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            self.client = anthropic.Anthropic()

    @property
    def mode(self) -> str:
        return "claude" if self.client else "rules"

    def ask(self, message: str, history: list[dict] | None = None) -> dict:
        message = (message or "").strip()
        if not message:
            raise ValueError("Empty message")
        if self.client:
            try:
                return self._ask_claude(message, history or [])
            except Exception as exc:  # fall back rather than fail the user's question
                out = self._ask_rules(message)
                out["warnings"].insert(0, f"Claude mode failed ({type(exc).__name__}); answered with the built-in parser instead.")
                return out
        return self._ask_rules(message)

    # -- claude --
    def _ask_claude(self, message: str, history: list[dict]) -> dict:
        msgs = [{"role": h["role"], "content": h["content"]} for h in history[-8:] if h.get("role") in ("user", "assistant") and h.get("content")]
        msgs.append({"role": "user", "content": message})
        steps, tables, warnings = [], [], []
        for _ in range(6):
            resp = self.client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT, tools=TOOLS, messages=msgs)
            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                return {"mode": "claude", "answer": text, "steps": steps, "tables": tables, "warnings": warnings}
            msgs.append({"role": "assistant", "content": resp.content})
            results = []
            for b in resp.content:
                if getattr(b, "type", "") != "tool_use":
                    continue
                try:
                    res = self.tools.call(b.name, b.input)
                    tables += tables_from(b.name, res)
                    warnings += res.get("warnings", [])
                    steps.append({"tool": b.name, "args": b.input, "summary": summarise(b.name, res), "sql": res.get("sql")})
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(_shrink(res), default=str)})
                except Exception as exc:
                    steps.append({"tool": b.name, "args": b.input, "summary": f"error: {exc}"})
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Error: {exc}", "is_error": True})
            msgs.append({"role": "user", "content": results})
        return {"mode": "claude", "answer": "I couldn't finish within the step limit - try a narrower question.", "steps": steps, "tables": tables, "warnings": warnings}

    # -- rules --
    def _ask_rules(self, message: str) -> dict:
        try:
            plan = parse_question(self.e, message)
        except ValueError as exc:
            return {"mode": "rules", "answer": str(exc), "steps": [], "tables": [], "warnings": []}
        if plan is None:
            return {"mode": "rules", "answer": HELP, "steps": [], "tables": [], "warnings": []}
        name, args = plan
        res = self.tools.call(name, args)
        tbl = tables_from(name, res)
        return {"mode": "rules", "answer": describe_result(name, args, res), "steps": [
            {"tool": name, "args": args, "summary": summarise(name, res), "sql": res.get("sql")}],
            "tables": tbl, "warnings": res.get("warnings", [])}


HELP = """I can answer questions like:
- best middle order batters by strike rate since 2022
- top 10 death bowlers by economy in the last 3 seasons
- top openers by average at Wankhede
- Kohli vs spin / how does Suryakumar play against pace
- Kohli vs Bumrah
- Bumrah against left handers
- players similar to Hardik Pandya
- venue profile for Chepauk
- profile of MS Dhoni
Add an ANTHROPIC_API_KEY to unlock free-form questions (Claude mode)."""

# ------------------------------------------------------------------ rules parser
TEAM_ALIASES = {
    "csk": "Chennai Super Kings", "chennai": "Chennai Super Kings", "mi": "Mumbai Indians", "mumbai indians": "Mumbai Indians",
    "rcb": "Royal Challengers Bengaluru", "kkr": "Kolkata Knight Riders", "srh": "Sunrisers Hyderabad",
    "dc": "Delhi Capitals", "pbks": "Punjab Kings", "rr": "Rajasthan Royals", "gt": "Gujarat Titans", "lsg": "Lucknow Super Giants",
}
STADIUM_ALIASES = {"chepauk": "chidambaram", "kotla": "kotla", "uppal": "rajiv gandhi", "vizag": "rajasekhara",
                   "motera": "narendra modi", "mohali": "bindra", "eden": "eden gardens", "chinnaswamy": "chinnaswamy",
                   "wankhede": "wankhede", "wankhede stadium": "wankhede"}
CITY_ALIASES = {"bengaluru": "chinnaswamy", "bangalore": "chinnaswamy", "jaipur": "sawai mansingh", "lucknow": "ekana",
                "ahmedabad": "narendra modi", "delhi": "arun jaitley", "kolkata": "eden gardens", "hyderabad": "rajiv gandhi",
                "chennai": "chidambaram", "mumbai": "wankhede", "dharamsala": "himachal", "guwahati": "barsapara",
                "pune": "maharashtra cricket association", "dubai": "dubai international", "sharjah": "sharjah"}
METRICS = [("wickets per over", "wickets_per_over"), ("strike rate", "strike_rate"), ("strike-rate", "strike_rate"),
           ("economy", "economy"), ("average", "average"), ("avg", "average"), ("boundary", "boundary_pct"),
           ("dot ball", "dot_pct"), ("dot%", "dot_pct"), ("wickets", "wickets"), ("sixes", "sixes"), ("runs", "runs"),
           ("bowling average", "bowling_average")]
ROLE_PATTERNS = [
    (r"middle[- ]overs? (bowler|bowlers)", "middle_overs_bowler"), (r"powerplay (bowler|bowlers)|new ball bowler", "powerplay_bowler"),
    (r"death (bowler|bowlers)", "death_bowler"), (r"\bpace(r|rs)?\b (bowler|bowlers)|\bpacers?\b|fast bowlers?", "pacer"),
    (r"\bspinners?\b|spin bowlers?", "spinner"), (r"middle[- ]order", "middle_order"), (r"top[- ]order", "top_order"),
    (r"\bopen(er|ers|ing)\b", "opener"), (r"\bfinishers?\b", "finisher"), (r"lower[- ]order|\btail\b", "lower_order"),
]
BOWL_WORDS = r"\b(bowler|bowlers|bowling|economy|wickets|pacers?|spinners?|wickets per over)\b"


def _filters(e: Engine, text: str) -> dict:
    t = text.lower()
    f: dict = {}
    if m := re.search(r"last (\d+) (seasons|years|editions)", t):
        f["last_n_seasons"] = int(m.group(1))
    elif re.search(r"last (season|year)|latest season|this season", t):
        f["last_n_seasons"] = 1
    if m := re.search(r"(?:from|between) (20\d\d)\s*(?:to|and|-)\s*(20\d\d)", t):
        f["season_from"], f["season_to"] = int(m.group(1)), int(m.group(2))
    elif m := re.search(r"since (20\d\d)", t):
        f["season_from"] = int(m.group(1))
    elif m := re.search(r"\b(?:in|during|for) (20\d\d)\b", t):
        f["season_from"] = f["season_to"] = int(m.group(1))
    if m := re.search(r"\b(powerplay|death|middle) (?:overs?|phase)\b|\bin the (powerplay|death|middle)\b", t):
        f["phase"] = m.group(1) or m.group(2)
    if re.search(r"\b(vs|against)\b.*\b(pace|pacers?|fast bowlers?)\b", t):
        f["bowler_kind"] = "pace"
    elif re.search(r"\b(vs|against)\b.*\b(spin|spinners?|spin bowlers?)\b", t):
        f["bowler_kind"] = "spin"
    if re.search(r"\b(vs|against)\b.*\b(left[- ]?hand(ers|ed)?|lefties)\b", t):
        f["batter_hand"] = "L"
    elif re.search(r"\b(vs|against)\b.*\b(right[- ]?hand(ers|ed)?)\b", t):
        f["batter_hand"] = "R"
    if re.search(r"\bchasing\b|second innings", t):
        f["innings"] = 2
    elif re.search(r"batting first|first innings", t):
        f["innings"] = 1
    for key, canon in STADIUM_ALIASES.items():
        if re.search(rf"\b{re.escape(key)}\b", t):
            f["venue"] = canon_venue(canon)
            break
    else:
        for key, canon in CITY_ALIASES.items():
            if re.search(rf"\b(at|in)\s+(the\s+)?{re.escape(key)}\b", t):
                f["venue"] = canon_venue(canon)
                break
        else:
            for canon, keys in VENUE_RULES:
                if any(k in t for k in keys):
                    f["venue"] = canon
                    break
    for alias, team in TEAM_ALIASES.items():
        if re.search(rf"\b(for|of) {re.escape(alias)}\b", t):
            f["team"] = team
        elif re.search(rf"\b(vs|against) {re.escape(alias)}\b", t):
            f["opposition"] = team
    return f


def _try_player(e: Engine, name: str):
    try:
        return e.resolve_player(name)
    except ValueError:
        return None


def parse_question(e: Engine, text: str):
    t = text.lower().strip().rstrip("?.! ")
    # --- venue profile
    if re.search(r"\b(venue|ground|stadium|pitch)\b", t) and re.search(r"\b(profile|stats|scoring|how does|chase|toss)\b", t):
        f = _filters(e, text)
        return "venue_profile", {"venue": f.pop("venue", None), "filters": f}
    # --- similar players
    if m := re.search(r"(?:similar to|players like|replacement for|replace|alternatives? to|like)\s+(.+)$", t):
        name = re.sub(r"\b(in|since|for|from|under)\b.*$", "", m.group(1)).strip()
        side = "bowl" if re.search(BOWL_WORDS, t) else "bat"
        pl = e.resolve_player(name)
        return "similar_players", {"player": pl, "side": side, "n": 8}
    # --- "<player> vs <something>"
    RANK_WORDS = ("best", "top", "who", "most", "highest", "lowest", "leading", "rank", "openers", "finishers", "bowlers", "batters")
    if m := re.match(r"^(.+?)\s+(?:vs|v|against)\s+(.+)$", t):
        a, b = m.group(1).strip(), m.group(2).strip()
        a = re.sub(r"^(how (does|do|did)|performance of|stats of|record of)\s+", "", a)
        a = re.sub(r"\s+(play|perform|bat|bowl|do|fare)$", "", a)
        b = re.sub(r"\b(in|since|at)\b.*$", "", b).strip()
        if not any(w in a.split() for w in RANK_WORDS):
            pa = _try_player(e, a)
            if pa and re.fullmatch(r"(pace|pacers?|fast bowlers?|spin|spinners?|spin bowlers?)", b):
                return "batter_vs_bowler_types", {"batter": pa, "filters": {}}
            if pa and re.fullmatch(r"(left[- ]?hand(ers|ed)?|right[- ]?hand(ers|ed)?|lefties)( batters?| batsmen)?", b):
                return "bowler_vs_batter_hands", {"bowler": pa, "filters": {}}
            pb = _try_player(e, b)
            if pa and pb:
                return "head_to_head", {"batter": pa, "bowler": pb}
    if m := re.search(r"how (?:does|do|did) (.+?) (?:play|bat|perform|fare) (?:against|vs|versus) (pace|spin)", t):
        return "batter_vs_bowler_types", {"batter": e.resolve_player(m.group(1)), "filters": {}}
    # --- player profile
    if m := re.match(r"^(?:profile|stats|summary|tell me about)(?: of| for)? (.+)$", t):
        return "player_profile", {"player": e.resolve_player(m.group(1).strip())}
    # --- rankings
    if re.search(r"\b(best|top|highest|lowest|rank|who|most|leading|better)\b", t):
        role = next((r for pat, r in ROLE_PATTERNS if re.search(pat, t)), None)
        side = "bowl" if (role in BOWL_ROLES or re.search(BOWL_WORDS, t)) else "bat"
        f = _filters(e, text)
        if role in ("powerplay_bowler", "middle_overs_bowler", "death_bowler") and "phase" not in f:
            f["phase"] = {"powerplay_bowler": "powerplay", "middle_overs_bowler": "middle", "death_bowler": "death"}[role]
        metric = next((m for k, m in METRICS if k in t), None)
        if side == "bat" and metric in ("economy", "wickets", "bowling_average", "wickets_per_over"):
            side = "bowl"
        if metric is None:
            metric = "economy" if side == "bowl" else "strike_rate"
        order = "asc" if re.search(r"\b(lowest|worst|least)\b", t) or (metric in ("economy", "bowling_average") and not re.search(r"highest", t)) else "desc"
        n = 10
        if m := re.search(r"\b(?:top|best)\s+(\d+)\b", t):
            n = int(m.group(1))
        args = {"side": side, "formula": metric, "filters": f, "limit": n, "order": order}
        if role:
            args["role"] = role
        if m := re.search(r"at least (\d+) balls", t):
            args["min_balls"] = int(m.group(1))
        return "rank_players", args
    return None


def describe_result(name: str, args: dict, res: dict) -> str:
    if name == "rank_players":
        rows = res["rows"]
        role = f" **{res['role']['role'].replace('_', ' ')}**" if res.get("role") else ""
        side = "batters" if res["side"] == "bat" else "bowlers"
        if not rows:
            return f"No{role} {side} matched ({res['filters_text']}). " + " ".join(res["warnings"])
        lead = rows[0]
        val = lead["score"]
        txt = f"Top{role} {side} by **{res['formula']}** ({res['filters_text']}, min {res['min_balls']} balls): " \
              f"**{lead['player']}** leads with {val:.2f}"
        if res["role"]:
            txt += f". Role rule: {res['role']['definition']} ({res['role']['qualifying_players']} players qualify)."
        return txt
    if name == "head_to_head":
        t = res["total"]
        return f"{res['batter']} vs {res['bowler']}: {t['runs']} runs off {t['balls']} balls, dismissed {t['dismissals']} time(s)."
    if name == "batter_vs_bowler_types":
        return f"{res['player']} against pace vs spin ({res['filters_text']})."
    if name == "bowler_vs_batter_hands":
        return f"{res['player']} against left- vs right-handed batters."
    if name == "venue_profile":
        return f"Venue profile ({res['filters_text']})."
    if name == "player_profile":
        return f"Profile of {res['player']} - inferred roles: {', '.join(res['roles']) or 'none (small sample)'}."
    if name == "similar_players":
        return f"Players statistically closest to {res['player']} ({res['filters_text']})."
    return "Done."
