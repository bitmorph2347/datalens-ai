"""FastAPI application: JSON API + static web UI."""
from __future__ import annotations

import io
import json
import re
import time
import uuid
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import budget, config, similarity
from .chat import ChatAgent, safe_select
from .engine import DEFAULT_TEMPLATE, Engine, records
from .reports import render_report, to_csv
from .uploads import MAX_MB, UploadStore


class RankReq(BaseModel):
    side: str = "bat"
    role: str | None = None
    formula: str | None = None
    filters: dict = Field(default_factory=dict)
    min_balls: int | None = None
    limit: int = 25
    order: str | None = None
    role_min_share: float | None = None


class MatchupReq(BaseModel):
    kind: str  # batter_vs_types | bowler_vs_hands | head_to_head | opponents
    batter: str | None = None
    bowler: str | None = None
    player: str | None = None
    side: str = "bat"
    filters: dict = Field(default_factory=dict)


class SimilarReq(BaseModel):
    player: str
    side: str = "bat"
    n: int = 10
    filters: dict = Field(default_factory=dict)
    max_price: float | None = None
    nationality: str | None = None
    exclude: list[str] = Field(default_factory=list)
    only_priced: bool = False
    active_only: bool = True
    weights: dict[str, float] = Field(default_factory=dict)


class SquadReq(BaseModel):
    squad: list[str]
    template: dict[str, int] | None = None
    filters: dict = Field(default_factory=dict)


class BudgetReq(BaseModel):
    purse: float = 50.0
    slots: dict[str, int] | None = None
    squad: list[str] = Field(default_factory=list)
    template: dict[str, int] | None = None
    max_overseas: int = 4
    filters: dict = Field(default_factory=dict)
    bat_formula: str | None = None
    bowl_formula: str | None = None
    exclude: list[str] = Field(default_factory=list)
    active_only: bool = True


class ChatReq(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)
    scope: dict = Field(default_factory=dict)


class SqlReq(BaseModel):
    query: str


class ExportReq(BaseModel):
    title: str = "DataLens export"
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    sections: list[dict] = Field(default_factory=list)


class ProjectReq(BaseModel):
    name: str
    kind: str = "analysis"
    spec: dict = Field(default_factory=dict)
    notes: str = ""


def create_app(engine: Engine | None = None, projects_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="DataLens AI", version="0.1.0",
                  description="Cricket analytics: roles, custom metrics, matchups, venues, auction tools.")
    engine = engine or Engine()
    agent = ChatAgent(engine)
    uploads = UploadStore()
    pdir = Path(projects_dir or config.PROJECTS_DIR)
    state = {"prices": None, "prices_source": "none"}

    def prices() -> pd.DataFrame:
        if state["prices"] is None:
            if not config.PRICES_CSV.exists():
                budget.make_sample_prices(engine)
            df, _ = budget.load_prices(engine)
            state["prices"], state["prices_source"] = df, "sample (synthetic, illustrative only)"
        return state["prices"]

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc.args[0] if isinstance(exc, KeyError) else exc)) from None

    # ---------------------------------------------------------------- meta
    @app.get("/api/meta")
    def meta():
        m = engine.meta()
        m["chat_mode"] = agent.mode
        m["max_upload_mb"] = MAX_MB
        m["default_template"] = DEFAULT_TEMPLATE
        return m

    @app.get("/api/players/search")
    def players(q: str, limit: int = 8):
        return engine.search_players(q, limit)

    # ---------------------------------------------------------------- analytics
    @app.post("/api/rank")
    def rank(req: RankReq):
        return guard(engine.rank, req.side, filters=req.filters, formula=req.formula, role=req.role,
                     role_min_share=req.role_min_share, min_balls=req.min_balls, limit=req.limit, order=req.order)

    @app.get("/api/profile/{player}")
    def profile(player: str):
        return guard(engine.player_profile, player)

    @app.post("/api/matchup")
    def matchup(req: MatchupReq):
        if req.kind == "batter_vs_types":
            return guard(engine.batter_vs_bowler_types, req.batter or req.player or "", req.filters)
        if req.kind == "bowler_vs_hands":
            return guard(engine.bowler_vs_batter_hands, req.bowler or req.player or "", req.filters)
        if req.kind == "head_to_head":
            return guard(engine.head_to_head, req.batter or "", req.bowler or "", req.filters)
        if req.kind == "opponents":
            return guard(engine.opponents, req.player or "", req.side, filters=req.filters)
        raise HTTPException(400, "Unknown matchup kind")

    @app.get("/api/venues")
    def venues(venue: str | None = None, season_from: int | None = None, season_to: int | None = None,
               format: str | None = None, gender: str | None = None, competition: str | None = None):
        f = {"season_from": season_from, "season_to": season_to, "format": format, "gender": gender,
             "competition": competition}
        return guard(engine.venue_profile, venue, f)

    @app.post("/api/similar")
    def similar(req: SimilarReq):
        pr = prices()
        cands = None
        if req.max_price is not None or req.only_priced:
            sel = pr if req.max_price is None else pr[pr.price_cr <= req.max_price]
            cands = sel.player.tolist()
        if req.active_only:
            active = engine.active_players(req.filters)
            cands = [p for p in (cands if cands is not None else engine.players) if p in active]
        res = guard(similarity.similar_players, engine, req.player, req.side, n=req.n, filters=req.filters,
                    weights=req.weights or None, candidates=cands, nationality=req.nationality, exclude=req.exclude)
        price_map = pr.set_index("player").price_cr.to_dict()
        for r in res["rows"]:
            r["price_cr"] = price_map.get(r["player"])
        res["price_source"] = state["prices_source"]
        return res

    @app.post("/api/squad/gaps")
    def gaps(req: SquadReq):
        return guard(engine.squad_gaps, req.squad, req.template, req.filters)

    # ---------------------------------------------------------------- auction
    @app.get("/api/prices")
    def get_prices(limit: int = 300):
        df = prices()
        return {"source": state["prices_source"], "count": len(df), "rows": records(df.head(limit))}

    @app.post("/api/prices/upload")
    async def upload_prices(file: UploadFile = File(...)):
        raw = await file.read()
        try:
            df = pd.read_csv(io.BytesIO(raw))
            df, unmatched = budget.load_prices(engine, df=df)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from None
        state["prices"], state["prices_source"] = df, f"uploaded: {file.filename}"
        return {"count": len(df), "unmatched": unmatched, "source": state["prices_source"]}

    @app.post("/api/budget")
    def optimise(req: BudgetReq):
        slots = req.slots
        extra: dict = {}
        if not slots:
            if not req.squad:
                raise HTTPException(400, "Give either role slots or an existing squad to derive gaps from.")
            g = guard(engine.squad_gaps, req.squad, req.template, req.filters)
            slots = g["gaps"]
            extra = {"derived_gaps": g}
            if not slots:
                raise HTTPException(400, "Your squad already covers the template - nothing to buy.")
        res = guard(budget.optimize, engine, prices(), req.purse, slots, max_overseas=req.max_overseas,
                    filters=req.filters, bat_formula=req.bat_formula, bowl_formula=req.bowl_formula,
                    exclude=req.exclude + req.squad, active_only=req.active_only)
        res["price_source"] = state["prices_source"]
        res["slots"] = slots
        return res | extra

    # ---------------------------------------------------------------- chat
    @app.post("/api/chat")
    def chat(req: ChatReq):
        return guard(agent.ask, req.message, req.history, req.scope)

    @app.post("/api/sql")
    def run_sql(req: SqlReq):
        try:
            sql = safe_select(req.query)
            df = engine.query(sql)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:
            raise HTTPException(400, f"SQL error: {exc}") from None
        return {"columns": list(df.columns), "rows": records(df)}

    # ---------------------------------------------------------------- uploads
    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...)):
        return guard(uploads.add, file.filename or "data.csv", await file.read())

    @app.get("/api/upload")
    def upload_list():
        return {"tables": uploads.names()}

    @app.get("/api/upload/{name}/profile")
    def upload_profile(name: str):
        return guard(uploads.profile, name)

    @app.post("/api/upload/{name}/clean")
    def upload_clean(name: str, fill: str = "none"):
        return guard(uploads.clean, name, fill)

    @app.post("/api/upload/sql")
    def upload_sql(req: SqlReq):
        return guard(uploads.query, req.query)

    @app.get("/api/upload/{name}/chart")
    def upload_chart(name: str, x: str, y: str | None = None, agg: str = "count"):
        return guard(uploads.chart, name, x, y, agg)

    # ---------------------------------------------------------------- projects + exports
    def _pfile(pid: str) -> Path:
        if not re.fullmatch(r"[a-z0-9\-]{6,40}", pid):
            raise HTTPException(400, "Bad project id")
        return pdir / f"{pid}.json"

    @app.get("/api/projects")
    def list_projects():
        pdir.mkdir(parents=True, exist_ok=True)
        out = []
        for p in sorted(pdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            d = json.loads(p.read_text())
            out.append({k: d.get(k) for k in ("id", "name", "kind", "created", "notes")})
        return out

    @app.post("/api/projects")
    def save_project(req: ProjectReq):
        pdir.mkdir(parents=True, exist_ok=True)
        pid = uuid.uuid4().hex[:12]
        data = {"id": pid, "name": req.name[:120], "kind": req.kind, "spec": req.spec, "notes": req.notes[:2000],
                "created": time.strftime("%Y-%m-%d %H:%M")}
        _pfile(pid).write_text(json.dumps(data))
        return data

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        f = _pfile(pid)
        if not f.exists():
            raise HTTPException(404, "Project not found")
        return json.loads(f.read_text())

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        f = _pfile(pid)
        if not f.exists():
            raise HTTPException(404, "Project not found")
        f.unlink()
        return {"deleted": pid}

    @app.post("/api/export/csv")
    def export_csv(req: ExportReq):
        cols = req.columns or (list(req.rows[0]) if req.rows else [])
        return PlainTextResponse(to_csv(cols, req.rows), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="datalens-export.csv"'})

    @app.post("/api/export/report")
    def export_report(req: ExportReq):
        sections = req.sections or [{"title": req.title, "columns": req.columns, "rows": req.rows}]
        return HTMLResponse(render_report(req.title, sections),
                            headers={"Content-Disposition": 'attachment; filename="datalens-report.html"'})

    # ---------------------------------------------------------------- web UI
    if config.WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(config.WEB_DIR / "index.html")

    @app.exception_handler(Exception)
    async def unhandled(_, exc: Exception):
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    return app


def get_app() -> FastAPI:  # for `uvicorn datalens.api:get_app --factory`
    return create_app()


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
