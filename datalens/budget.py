"""Auction budget optimizer: pick the best squad additions for role gaps within a purse."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp

from . import config
from .engine import BAT_ROLES, BOWL_ROLES, Engine, Filters, norm_role, records

DEFAULT_BAT_FORMULA = "0.5*z(strike_rate) + 0.3*z(average) + 0.2*z(boundary_pct)"
DEFAULT_BOWL_FORMULA = "0.5*z(wickets_per_over) - 0.5*z(economy)"


def load_prices(engine: Engine, path=None, df: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Load a price list (columns: player, price_cr[, nationality]). Names are fuzzily matched to Cricsheet names."""
    if df is None:
        df = pd.read_csv(path or config.PRICES_CSV)
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    if "player" not in df or "price_cr" not in df:
        raise ValueError("Price file needs columns: player, price_cr (optional: nationality)")
    unmatched, names = [], []
    for raw in df["player"].astype(str):
        hit = raw if raw in engine._player_set else engine.resolver.resolve(raw, 0.8)
        names.append(hit)
        if not hit:
            unmatched.append(raw)
    df["player"] = names
    df = df.dropna(subset=["player"]).drop_duplicates("player")
    df["price_cr"] = pd.to_numeric(df["price_cr"], errors="coerce")
    df = df.dropna(subset=["price_cr"])
    if "nationality" not in df:
        meta = engine.query("SELECT player, nationality FROM players_meta")
        df = df.merge(meta, on="player", how="left")
    df["nationality"] = df["nationality"].fillna("UNK").str.upper()
    return df, unmatched


def make_sample_prices(engine: Engine, path=config.PRICES_CSV) -> pd.DataFrame:
    """Synthetic, clearly-labelled prices so the optimizer works out of the box. NOT real auction data."""
    f = Filters(last_n_seasons=3)
    q = engine.query(f"""
        WITH bt AS (SELECT striker p, sum(runs_off_bat) runs FROM balls WHERE season BETWEEN {engine._seasons(f)[0]} AND {engine._seasons(f)[1]} GROUP BY 1),
             bw AS (SELECT bowler p, sum(bowler_wicket::INT) w, sum(legal_ball::INT) lb FROM balls WHERE season BETWEEN {engine._seasons(f)[0]} AND {engine._seasons(f)[1]} GROUP BY 1)
        SELECT COALESCE(bt.p, bw.p) AS player, COALESCE(bt.runs, 0) + 22 * COALESCE(bw.w, 0) AS impact,
               COALESCE(bt.runs, 0) AS runs, COALESCE(bw.lb, 0) AS lb
        FROM bt FULL JOIN bw ON bt.p = bw.p WHERE COALESCE(bt.runs, 0) + COALESCE(bw.lb, 0) >= 60""")
    q["pct"] = q.impact.rank(pct=True)
    q["price_cr"] = (0.3 + 17.7 * q.pct ** 3).round(2)
    meta = engine.query("SELECT player, nationality FROM players_meta")
    q = q.merge(meta, on="player", how="left")
    out = q[["player", "price_cr", "nationality"]].sort_values("price_cr", ascending=False)
    out["nationality"] = out["nationality"].fillna("UNK")
    out["source"] = "synthetic-illustrative"
    out.to_csv(path, index=False)
    return out


def optimize(engine: Engine, prices: pd.DataFrame, purse: float, slots: dict[str, int], *,
             max_overseas: int = 4, filters: dict | None = None, bat_formula: str | None = None,
             bowl_formula: str | None = None, pool_size: int = 40, exclude: list[str] | None = None,
             min_balls_bat: int | None = None, min_balls_bowl: int | None = None,
             active_only: bool = True) -> dict:
    """Maximise total role-fit score within the purse, slot counts per role and overseas cap (exact MILP)."""
    f = Filters.from_dict(filters)
    if not (f.last_n_seasons or f.season_from):
        f.last_n_seasons = 3
    slots = {norm_role(k): int(v) for k, v in slots.items() if int(v) > 0}
    if not slots:
        raise ValueError("No slots requested.")
    for r in slots:
        if r not in BAT_ROLES and r not in BOWL_ROLES:
            raise ValueError(f"Unknown role '{r}'")
    exclude_set = {engine.resolver.resolve(e, 0.8) or e for e in (exclude or [])}
    price = prices.set_index("player")
    pool_players = [p for p in price.index if p not in exclude_set]
    if active_only:
        active = engine.active_players()
        pool_players = [p for p in pool_players if p in active]
    cand: list[dict] = []
    warnings: list[str] = []
    for role in slots:
        side = "bat" if role in BAT_ROLES else "bowl"
        formula = (bat_formula or DEFAULT_BAT_FORMULA) if side == "bat" else (bowl_formula or DEFAULT_BOWL_FORMULA)
        res = engine.rank(side, filters=f, role=role, formula=formula, limit=200, players=pool_players,
                          min_balls=(min_balls_bat if side == "bat" else min_balls_bowl) or (100 if side == "bat" else 90),
                          min_role_innings=5)
        rows = res["rows"][:pool_size]
        if not rows:
            warnings.append(f"No priced candidates found for role '{role}'.")
            continue
        scores = pd.Series([r["score"] for r in rows])
        pct = scores.rank(pct=True).values * 100  # percentile within role so roles are comparable
        for r, s in zip(rows, pct):
            cand.append({"player": r["player"], "role": role, "score": float(s), "raw_score": r["score"],
                         "price": float(price.loc[r["player"], "price_cr"]),
                         "overseas": price.loc[r["player"], "nationality"] == "OS",
                         "nationality": price.loc[r["player"], "nationality"]})
    if not cand:
        raise ValueError("No candidates found. Check the price list and role names.")
    c = pd.DataFrame(cand)
    n = len(c)

    def solve(exact: bool):
        A, lo, hi = [], [], []
        for p in c.player.unique():  # a player fills at most one slot
            A.append((c.player == p).astype(float).values); lo.append(0); hi.append(1)
        for role, k in slots.items():
            A.append((c.role == role).astype(float).values)
            lo.append(k if exact else 0); hi.append(k)
        A.append(c.price.values.astype(float)); lo.append(0); hi.append(purse)
        A.append(c.overseas.astype(float).values); lo.append(0); hi.append(max_overseas)
        return milp(c=-c.score.values, constraints=LinearConstraint(np.array(A), lo, hi),
                    integrality=np.ones(n), bounds=Bounds(0, 1))

    res = solve(True)
    relaxed = False
    if not res.success:
        res = solve(False)
        relaxed = True
        warnings.append("Could not fill every slot within the purse/overseas limit; showing the best partial squad.")
    if not res.success:
        raise ValueError("No feasible selection. Try a bigger purse or fewer slots.")
    pick = c[np.round(res.x) == 1].sort_values(["role", "score"], ascending=[True, False])
    filled = pick.groupby("role").size().to_dict()
    unfilled = {r: k - filled.get(r, 0) for r, k in slots.items() if filled.get(r, 0) < k}
    # next-best alternatives per slot: top unselected candidates by value
    alts = c[~c.player.isin(pick.player)].assign(value=lambda d: d.score / d.price.clip(lower=0.3)) \
        .sort_values("value", ascending=False).head(8)
    unk = int((pick.nationality == "UNK").sum())
    if unk:
        warnings.append(f"{unk} selected player(s) have unknown nationality and were treated as Indian; check the overseas cap.")
    return {
        "purse": purse, "spent": round(float(pick.price.sum()), 2), "remaining": round(float(purse - pick.price.sum()), 2),
        "overseas_used": int(pick.overseas.sum()), "max_overseas": max_overseas, "total_score": round(float(pick.score.sum()), 1),
        "selected": records(pick[["player", "role", "score", "raw_score", "price", "nationality"]]),
        "unfilled": unfilled, "alternatives": records(alts[["player", "role", "score", "price", "nationality"]]),
        "filters_text": f.describe(), "optimal": not relaxed, "warnings": warnings,
    }
