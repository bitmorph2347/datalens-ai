"""Player similarity and replacement finder."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import Engine, Filters, records, _Q

BAT_FEATURES = ["strike_rate", "average", "boundary_pct", "dot_pct", "avg_pos", "pp_share", "death_share",
                "sr_vs_pace", "sr_vs_spin"]
BOWL_FEATURES = ["economy", "bowling_sr", "dot_pct", "boundary_pct", "pp_share", "middle_share", "death_share", "is_pace"]


def feature_table(engine: Engine, side: str, f: Filters, min_balls: int) -> pd.DataFrame:
    q = _Q()
    if side == "bat":
        sql = f"""
        SELECT b.striker AS player, sum(b.runs_off_bat) AS runs, sum(b.batter_faced::INT) AS balls,
               sum(b.striker_out::INT) AS outs, sum(b.is_boundary::INT) AS bnd,
               sum((b.batter_faced AND b.runs_off_bat = 0)::INT) AS dots,
               avg(b.striker_pos) AS avg_pos,
               sum((b.batter_faced AND b.phase = 'powerplay')::INT) * 1.0 / sum(b.batter_faced::INT) AS pp_share,
               sum((b.batter_faced AND b.phase = 'death')::INT) * 1.0 / sum(b.batter_faced::INT) AS death_share,
               sum(CASE WHEN pm.bowl_kind = 'pace' THEN b.runs_off_bat END) * 100.0 /
                   NULLIF(sum((pm.bowl_kind = 'pace' AND b.batter_faced)::INT), 0) AS sr_vs_pace,
               sum(CASE WHEN pm.bowl_kind = 'spin' THEN b.runs_off_bat END) * 100.0 /
                   NULLIF(sum((pm.bowl_kind = 'spin' AND b.batter_faced)::INT), 0) AS sr_vs_spin
        FROM balls b LEFT JOIN players_meta pm ON pm.player = b.bowler
        WHERE {engine._scope(f, q)}
        GROUP BY b.striker HAVING sum(b.batter_faced::INT) >= {q.p(min_balls)}"""
        df = engine.query(sql, q.params)
        df["strike_rate"] = df.runs * 100.0 / df.balls
        df["average"] = (df.runs / df.outs.where(df.outs > 0)).fillna(df.runs).clip(upper=80)
        df["boundary_pct"] = df.bnd * 100.0 / df.balls
        df["dot_pct"] = df.dots * 100.0 / df.balls
        df["sr_vs_pace"] = df.sr_vs_pace.fillna(df.strike_rate)
        df["sr_vs_spin"] = df.sr_vs_spin.fillna(df.strike_rate)
        return df.set_index("player")[BAT_FEATURES + ["balls"]]
    sql = f"""
    SELECT b.bowler AS player, sum(b.legal_ball::INT) AS balls, sum(b.runs_conceded) AS runs,
           sum(b.bowler_wicket::INT) AS wkts, sum((b.legal_ball AND b.runs_conceded = 0)::INT) AS dots,
           sum(b.is_boundary::INT) AS bnd,
           sum((b.legal_ball AND b.phase = 'powerplay')::INT) * 1.0 / sum(b.legal_ball::INT) AS pp_share,
           sum((b.legal_ball AND b.phase = 'middle')::INT) * 1.0 / sum(b.legal_ball::INT) AS middle_share,
           sum((b.legal_ball AND b.phase = 'death')::INT) * 1.0 / sum(b.legal_ball::INT) AS death_share,
           any_value(CASE pm.bowl_kind WHEN 'pace' THEN 1.0 WHEN 'spin' THEN 0.0 ELSE 0.5 END) AS is_pace
    FROM balls b LEFT JOIN players_meta pm ON pm.player = b.bowler
    WHERE {engine._scope(f, q)}
    GROUP BY b.bowler HAVING sum(b.legal_ball::INT) >= {q.p(min_balls)}"""
    df = engine.query(sql, q.params)
    df["economy"] = df.runs * 6.0 / df.balls
    df["bowling_sr"] = (df.balls / df.wkts.where(df.wkts > 0)).fillna(60).clip(upper=60)
    df["dot_pct"] = df.dots * 100.0 / df.balls
    df["boundary_pct"] = df.bnd * 100.0 / df.balls
    return df.set_index("player")[BOWL_FEATURES + ["balls"]]


def similar_players(engine: Engine, player: str, side: str = "bat", n: int = 10, filters: dict | None = None,
                    min_balls: int | None = None, weights: dict[str, float] | None = None,
                    candidates: list[str] | None = None, nationality: str | None = None,
                    exclude: list[str] | None = None) -> dict:
    """Players with the most similar statistical profile (role, phase usage, strike rate / economy...)."""
    player = engine.resolve_player(player)
    f = Filters.from_dict(filters)
    if not (f.last_n_seasons or f.season_from):
        f.last_n_seasons = 3
    min_balls = min_balls or ((100 if side == "bat" else 90) * (2 if f.format == "ODI" else 1))
    feats = feature_table(engine, side, f, min_balls)
    if player not in feats.index:
        raise ValueError(f"{player} has fewer than {min_balls} {'balls faced' if side == 'bat' else 'balls bowled'} "
                         f"in the chosen window ({f.describe()}), so a reliable profile can't be built.")
    cols = [c for c in feats.columns if c != "balls"]
    X = feats[cols]
    z = (X - X.mean()) / X.std(ddof=0).replace(0, 1)
    w = np.array([float((weights or {}).get(c, 1.0)) for c in cols])
    diff = (z - z.loc[player]) * np.sqrt(w)
    dist = np.sqrt((diff ** 2).sum(axis=1)) / np.sqrt(w.sum())
    res = pd.DataFrame({"player": z.index, "distance": dist.values, "balls": feats["balls"].values})
    res["similarity"] = (100.0 / (1.0 + res.distance)).round(1)
    res = res[res.player != player]
    if candidates is not None:
        cset = {engine.resolver.resolve(c, 0.8) or c for c in candidates}
        res = res[res.player.isin(cset)]
    if exclude:
        res = res[~res.player.isin({engine.resolver.resolve(e, 0.8) or e for e in exclude})]
    meta = engine.query("SELECT player, nationality, bat_hand, bowl_style FROM players_meta")
    res = res.merge(meta, on="player", how="left")
    if nationality:
        res = res[res.nationality.fillna("UNK") == nationality.upper()]
    res = res.sort_values("distance").head(max(1, min(n, 50)))
    # "why": the features on which each candidate is closest to the target
    rows = []
    for _, r in res.iterrows():
        gap = (z.loc[r.player] - z.loc[player]).abs().sort_values()
        close = gap.index[:3].tolist()
        d = {k: (None if pd.isna(v) else v) for k, v in r.to_dict().items()}
        d["closest_on"] = close
        d["stats"] = {c: round(float(X.loc[r.player, c]), 2) for c in cols}
        rows.append(d)
    return {"player": player, "side": side, "filters_text": f.describe(), "features": cols,
            "target_stats": {c: round(float(X.loc[player, c]), 2) for c in cols},
            "rows": records(pd.DataFrame(rows)) if rows else [],
            "warnings": ["Similarity compares statistical profiles; it does not account for age, fitness or price."]}
