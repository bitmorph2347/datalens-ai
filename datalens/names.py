"""Normalisation of team / venue names and fuzzy player-name matching."""
from __future__ import annotations

import difflib
import re

TEAM_CANON = {
    "Delhi Daredevils": "Delhi Capitals",
    "Kings XI Punjab": "Punjab Kings",
    "Royal Challengers Bangalore": "Royal Challengers Bengaluru",
    "Rising Pune Supergiants": "Rising Pune Supergiant",
}

# (canonical venue, lowercase substrings that identify it)
VENUE_RULES = [
    ("Arun Jaitley Stadium, Delhi", ["arun jaitley", "feroz shah kotla"]),
    ("Barabati Stadium, Cuttack", ["barabati"]),
    ("Barsapara Cricket Stadium, Guwahati", ["barsapara"]),
    ("Ekana Cricket Stadium, Lucknow", ["ekana"]),
    ("Brabourne Stadium, Mumbai", ["brabourne"]),
    ("Buffalo Park, East London", ["buffalo park"]),
    ("De Beers Diamond Oval, Kimberley", ["de beers"]),
    ("Dr DY Patil Sports Academy, Mumbai", ["dy patil"]),
    ("YSR ACA-VDCA Stadium, Visakhapatnam", ["rajasekhara"]),
    ("Dubai International Cricket Stadium", ["dubai international"]),
    ("Eden Gardens, Kolkata", ["eden gardens"]),
    ("Green Park, Kanpur", ["green park"]),
    ("HPCA Stadium, Dharamsala", ["himachal"]),
    ("Holkar Cricket Stadium, Indore", ["holkar"]),
    ("JSCA International Stadium, Ranchi", ["jsca"]),
    ("Kingsmead, Durban", ["kingsmead"]),
    ("M Chinnaswamy Stadium, Bengaluru", ["chinnaswamy"]),
    ("MA Chidambaram Stadium, Chennai", ["chidambaram"]),
    ("Maharaja Yadavindra Singh Stadium, Mullanpur", ["maharaja yadavindra"]),
    ("MCA Stadium, Pune", ["maharashtra cricket association", "subrata roy"]),
    ("Narendra Modi Stadium, Ahmedabad", ["narendra modi", "sardar patel"]),
    ("Nehru Stadium, Kochi", ["nehru stadium"]),
    ("New Wanderers Stadium, Johannesburg", ["new wanderers"]),
    ("Newlands, Cape Town", ["newlands"]),
    ("OUTsurance Oval, Bloemfontein", ["outsurance"]),
    ("IS Bindra Stadium, Mohali", ["bindra", "punjab cricket association"]),
    ("Rajiv Gandhi International Stadium, Hyderabad", ["rajiv gandhi"]),
    ("Sawai Mansingh Stadium, Jaipur", ["sawai mansingh"]),
    ("Shaheed Veer Narayan Singh Stadium, Raipur", ["shaheed veer"]),
    ("Sharjah Cricket Stadium", ["sharjah"]),
    ("Sheikh Zayed Stadium, Abu Dhabi", ["zayed"]),
    ("St George's Park, Gqeberha", ["st george"]),
    ("SuperSport Park, Centurion", ["supersport"]),
    ("Vidarbha Cricket Association Stadium, Nagpur", ["vidarbha"]),
    ("Wankhede Stadium, Mumbai", ["wankhede"]),
]

BOWL_KIND = {"RAP": "pace", "LAP": "pace", "OB": "spin", "LB": "spin", "SLA": "spin", "LCH": "spin"}


def canon_team(name: str | None) -> str | None:
    return TEAM_CANON.get(name, name) if name else name


def canon_venue(name: str | None) -> str | None:
    if not name:
        return name
    low = name.lower().replace(".", "")
    for canon, keys in VENUE_RULES:
        if any(k in low for k in keys):
            return canon
    return name


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z ]", "", s.lower().replace("-", " ")).strip()


class PlayerResolver:
    """Resolve free-text player names (e.g. 'Virat Kohli', 'kohli') to Cricsheet names ('V Kohli')."""

    def __init__(self, players: list[str], weights: dict[str, float] | None = None):
        self.players = list(players)
        self._norm = {p: norm_text(p) for p in self.players}
        # Activity weights break ties, so "Kohli" resolves to the famous V Kohli, not a namesake.
        self._w = weights or {}

    def _score(self, query: str, cand: str) -> float:
        q, c = norm_text(query), self._norm[cand]
        if not q:
            return 0.0
        if q == c:
            return 1.0
        q_parts, c_parts = q.split(), c.split()
        score = difflib.SequenceMatcher(None, q, c).ratio()
        # Surname + first initial heuristic: "virat kohli" vs "v kohli"
        if len(q_parts) >= 2 and len(c_parts) >= 2 and q_parts[-1] == c_parts[-1]:
            init_ok = c_parts[0][0] == q_parts[0][0] or all(ch == q_parts[0][0] for ch in c_parts[0][:1])
            score = max(score, 0.93 if init_ok else 0.8)
        elif len(q_parts) == 1 and q_parts[0] == c_parts[-1]:
            score = max(score, 0.85)
        elif q in c:
            score = max(score, 0.75)
        return score

    def search(self, query: str, limit: int = 5) -> list[tuple[str, float]]:
        scored = [(p, self._score(query, p)) for p in self.players]
        scored = [x for x in scored if x[1] >= 0.55]
        scored.sort(key=lambda x: (-round(x[1], 2), -self._w.get(x[0], 0), x[0]))
        return scored[:limit]

    def resolve(self, query: str, threshold: float = 0.8) -> str | None:
        hits = self.search(query, 1)
        return hits[0][0] if hits and hits[0][1] >= threshold else None
