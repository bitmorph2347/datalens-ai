"""FastAPI application: JSON API + static web UI."""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import budget, config, similarity
from .chat import ChatAgent, safe_select
from .engine import DEFAULT_TEMPLATE, Engine, Filters, records
from .reports import render_report, to_csv
from .rosters import RosterStore
from .sessions import Session, SessionError, SessionManager


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
    squad: list[str] = Field(max_length=80)
    template: dict[str, int] | None = None
    filters: dict = Field(default_factory=dict)


class BudgetReq(BaseModel):
    purse: float = 50.0
    slots: dict[str, int] | None = None
    squad: list[str] = Field(default_factory=list, max_length=80)
    template: dict[str, int] | None = None
    max_overseas: int = 4
    filters: dict = Field(default_factory=dict)
    bat_formula: str | None = None
    bowl_formula: str | None = None
    exclude: list[str] = Field(default_factory=list)
    active_only: bool = True
    team: str | None = None          # your team: other teams' players are then excluded
    use_roster: bool = True          # exclude players on other teams (needs a competition and team)
    free: list[str] = Field(default_factory=list, max_length=300)    # released / free players (override other teams' lists)
    taken: list[str] = Field(default_factory=list, max_length=300)   # not available: signed elsewhere, retired, injured


class RosterSaveReq(BaseModel):
    filters: dict = Field(default_factory=dict)
    season: int
    team: str
    retained: list[str] = Field(default_factory=list, max_length=80)
    free: list[str] = Field(default_factory=list, max_length=300)
    taken: list[str] = Field(default_factory=list, max_length=300)


class ChatReq(BaseModel):
    message: str = Field(max_length=2000)
    history: list[dict] = Field(default_factory=list, max_length=20)
    scope: dict = Field(default_factory=dict)


class SqlReq(BaseModel):
    query: str


class ExportReq(BaseModel):
    title: str = "DataLens export"
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list, max_length=20000)
    sections: list[dict] = Field(default_factory=list, max_length=50)


class ProjectReq(BaseModel):
    name: str
    kind: str = "analysis"
    spec: dict = Field(default_factory=dict)
    notes: str = ""


HEAVY_PREFIXES = ("/api/budget", "/api/similar", "/api/chat", "/api/upload", "/api/squad", "/api/rosters/upload",
                  "/api/prices/upload", "/api/export")
SMALL_FILE = 2 * 1024 * 1024  # price lists and roster files


class RateLimiter:
    """Sliding-window limiter per client key (public mode only)."""

    def __init__(self, general: int, heavy: int, window: float = 60.0):
        self.general, self.heavy, self.window = general, heavy, window
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, heavy: bool) -> bool:
        now = time.time()
        with self._lock:
            if len(self._hits) > 5000:  # forget idle clients
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1][0] < self.window}
            q = self._hits.setdefault(key, deque())  # entries are (timestamp, was_heavy)
            while q and now - q[0][0] > self.window:
                q.popleft()
            if len(q) >= self.general:
                return False
            if heavy and sum(1 for _, was_heavy in q if was_heavy) >= self.heavy:
                return False
            q.append((now, heavy))
            return True


async def read_capped(file: UploadFile, limit: int) -> bytes:
    """Read an upload in chunks and refuse it as soon as it exceeds ``limit`` bytes."""
    chunks, size = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"File too large (max {limit // (1024 * 1024)} MB).")
        chunks.append(chunk)
    return b"".join(chunks)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                                "object-src 'none'; base-uri 'self'; form-action 'self'; "
                                "frame-ancestors 'self' https://huggingface.co https://*.huggingface.co https://*.hf.space"),
}


def create_app(engine: Engine | None = None, projects_dir: Path | None = None, rosters_path: Path | None = None,
               public: bool | None = None, session_dir: Path | None = None, rate_general: int | None = None,
               rate_heavy: int | None = None, max_sessions: int = 300) -> FastAPI:
    """Build the app. ``public`` (or DATALENS_PUBLIC=1) turns on per-visitor sessions, rate limits, caps and headers."""
    public = (os.environ.get("DATALENS_PUBLIC") == "1") if public is None else public
    app = FastAPI(title="DataLens AI", version="0.2.0", docs_url=None if public else "/docs",
                  redoc_url=None, openapi_url=None if public else "/openapi.json",
                  description="Cricket analytics: roles, custom metrics, matchups, venues, auction tools.")
    engine = engine or Engine()
    allow_llm = (not public) or os.environ.get("DATALENS_ALLOW_LLM") == "1"
    agent = ChatAgent(engine, allow_llm=allow_llm)
    mgr = SessionManager(engine, public=public, projects_dir=Path(projects_dir or config.PROJECTS_DIR),
                         rosters_path=Path(rosters_path or config.ROSTERS_CSV), base_dir=session_dir,
                         max_sessions=max_sessions)
    limiter = RateLimiter(rate_general or int(os.environ.get("DATALENS_RATE_GENERAL", "150")),
                          rate_heavy or int(os.environ.get("DATALENS_RATE_HEAVY", "30")))
    default_prices: dict = {}

    def current(request: Request) -> Session:
        try:
            return mgr.get(request.headers.get("x-session-id"))
        except SessionError as exc:
            raise HTTPException(400, str(exc)) from None

    def prices(s: Session) -> pd.DataFrame:
        if s.prices is None:
            if "df" not in default_prices:
                if not config.PRICES_CSV.exists():
                    budget.make_sample_prices(engine)
                default_prices["df"], _ = budget.load_prices(engine)
            s.prices, s.prices_source = default_prices["df"], "sample (synthetic, illustrative only)"
        return s.prices

    def client_key(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return fwd or (request.client.host if request.client else "unknown")

    if public:
        @app.middleware("http")
        async def public_guard(request: Request, call_next):
            path = request.url.path
            if not path.startswith("/static") and path != "/api/health":
                heavy = request.method != "GET" and path.startswith(HEAVY_PREFIXES)
                if not limiter.allow(client_key(request), heavy):
                    return JSONResponse({"detail": "Too many requests - please slow down and try again in a minute."},
                                        status_code=429, headers={"Retry-After": "30"})
                try:
                    size = int(request.headers.get("content-length") or 0)
                except ValueError:
                    size = 0
                cap = (mgr.upload_mb * 1024 * 1024 + 1024 * 1024) if path.startswith("/api/upload") else 4 * 1024 * 1024
                if size > cap:
                    return JSONResponse({"detail": "Request too large."}, status_code=413)
            resp = await call_next(request)
            for k, v in SECURITY_HEADERS.items():
                resp.headers[k] = v
            if path.startswith("/api/"):
                resp.headers["Cache-Control"] = "no-store"
            return resp

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
        m["max_upload_mb"] = mgr.upload_mb
        m["public"] = public
        m["default_template"] = DEFAULT_TEMPLATE
        return m

    @app.get("/api/health")
    def health():
        return {"status": "ok", "public": public, "sessions": mgr.count()}

    @app.get("/api/players/search")
    def players(q: str = Query(max_length=100), limit: int = Query(8, ge=1, le=25)):
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
    def similar(req: SimilarReq, request: Request):
        s = current(request)
        pr = prices(s)
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
        res["price_source"] = s.prices_source
        return res

    @app.post("/api/squad/gaps")
    def gaps(req: SquadReq):
        return guard(engine.squad_gaps, req.squad, req.template, req.filters)

    # ---------------------------------------------------------------- auction
    @app.get("/api/prices")
    def get_prices(request: Request, limit: int = Query(300, ge=1, le=2000)):
        s = current(request)
        df = prices(s)
        return {"source": s.prices_source, "count": len(df), "rows": records(df.head(limit))}

    @app.post("/api/prices/upload")
    async def upload_prices(request: Request, file: UploadFile = File(...)):
        s = current(request)
        raw = await read_capped(file, SMALL_FILE)
        try:
            df = pd.read_csv(io.BytesIO(raw))
            df, unmatched = budget.load_prices(engine, df=df)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from None
        s.prices, s.prices_source = df, f"uploaded: {(file.filename or 'file')[:80]}"
        return {"count": len(df), "unmatched": unmatched, "source": s.prices_source}

    @app.post("/api/budget")
    def optimise(req: BudgetReq, request: Request):
        s = current(request)
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
        exclude, active_only, roster_info = req.exclude + req.squad, req.active_only, None
        if req.use_roster and req.team:
            un = guard(s.rosters.unavailable, Filters.from_dict(req.filters), req.team, req.free, req.taken)
            exclude = exclude + sorted(un["players"])
            active_only = False  # released / unsigned players did not play the latest season for any team
            roster_info = {"team": req.team, "excluded_players": len(un["players"]), "other_teams": un["other_teams"],
                           "unresolved": un["unresolved"], "latest_season": un["latest_season"],
                           "file_season": un["file_season"],
                           "note": ("Players on the other teams' squads were excluded "
                                    + (f"(roster file {un['file_season']} where listed, otherwise auto-guessed from the "
                                       f"{un['latest_season']} season). " if un["file_season"] else
                                       f"(auto-guessed from the {un['latest_season']} season; save a roster file to make this exact). ")
                                    + "Only players seen in the latest or previous season are suggested (the data can't know about retirements); check the 'last season' column.")}
        res = guard(budget.optimize, engine, prices(s), req.purse, slots, max_overseas=req.max_overseas,
                    filters=req.filters, bat_formula=req.bat_formula, bowl_formula=req.bowl_formula,
                    exclude=exclude, active_only=active_only,
                    min_last_season=(roster_info or {}).get("latest_season", 0) - 1 or None)
        res["roster"] = roster_info
        res["price_source"] = s.prices_source
        res["slots"] = slots
        return res | extra

    # ---------------------------------------------------------------- rosters
    @app.get("/api/rosters/teams")
    def roster_teams(request: Request, competition: str | None = None, format: str | None = None, gender: str | None = None):
        return guard(current(request).rosters.teams, Filters.from_dict({"competition": competition, "format": format, "gender": gender}))

    @app.get("/api/rosters/squad")
    def roster_squad(request: Request, team: str, competition: str | None = None, format: str | None = None, gender: str | None = None):
        return guard(current(request).rosters.team_squad, Filters.from_dict({"competition": competition, "format": format, "gender": gender}), team)

    @app.post("/api/rosters/save")
    def roster_save(req: RosterSaveReq, request: Request):
        return guard(current(request).rosters.save, Filters.from_dict(req.filters), req.season, req.team, req.retained, req.free, req.taken)

    @app.post("/api/rosters/upload")
    async def roster_upload(request: Request, file: UploadFile = File(...)):
        s = current(request)
        raw = await read_capped(file, SMALL_FILE)
        try:
            df = pd.read_csv(io.BytesIO(raw), dtype=str)
        except Exception as exc:
            raise HTTPException(400, f"Could not read the file: {exc}") from None
        return guard(s.rosters.upload, df)

    # ---------------------------------------------------------------- chat
    @app.post("/api/chat")
    def chat(req: ChatReq):
        return guard(agent.ask, req.message, req.history, req.scope)

    @app.post("/api/sql")
    def run_sql(req: SqlReq):
        if public:
            raise HTTPException(403, "Raw SQL over the cricket database is disabled on the public site.")
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
    async def upload(request: Request, file: UploadFile = File(...)):
        s = current(request)
        up = s.uploads
        raw = await read_capped(file, up.max_bytes)
        return guard(up.add, (file.filename or "data.csv")[:120], raw)

    @app.get("/api/upload")
    def upload_list(request: Request):
        return {"tables": current(request).uploads.names()}

    @app.get("/api/upload/{name}/profile")
    def upload_profile(name: str, request: Request):
        return guard(current(request).uploads.profile, name)

    @app.post("/api/upload/{name}/clean")
    def upload_clean(name: str, request: Request, fill: str = "none"):
        return guard(current(request).uploads.clean, name, fill)

    @app.post("/api/upload/sql")
    def upload_sql(req: SqlReq, request: Request):
        return guard(current(request).uploads.query, req.query)

    @app.get("/api/upload/{name}/chart")
    def upload_chart(name: str, request: Request, x: str, y: str | None = None, agg: str = "count"):
        return guard(current(request).uploads.chart, name, x, y, agg)

    # ---------------------------------------------------------------- projects + exports
    def _pfile(s: Session, pid: str) -> Path:
        if not re.fullmatch(r"[a-z0-9\-]{6,40}", pid):
            raise HTTPException(400, "Bad project id")
        return s.projects_dir / f"{pid}.json"

    @app.get("/api/projects")
    def list_projects(request: Request):
        pdir = current(request).projects_dir
        pdir.mkdir(parents=True, exist_ok=True)
        out = []
        for p in sorted(pdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            d = json.loads(p.read_text())
            out.append({k: d.get(k) for k in ("id", "name", "kind", "created", "notes")})
        return out

    @app.post("/api/projects")
    def save_project(req: ProjectReq, request: Request):
        s = current(request)
        s.projects_dir.mkdir(parents=True, exist_ok=True)
        if public and (len(list(s.projects_dir.glob("*.json"))) >= 50 or len(json.dumps(req.spec)) > 100_000):
            raise HTTPException(400, "Project limit reached (50 projects, 100 KB each). Delete some first.")
        pid = uuid.uuid4().hex[:12]
        data = {"id": pid, "name": req.name[:120], "kind": req.kind, "spec": req.spec, "notes": req.notes[:2000],
                "created": time.strftime("%Y-%m-%d %H:%M")}
        _pfile(s, pid).write_text(json.dumps(data))
        return data

    @app.get("/api/projects/{pid}")
    def get_project(pid: str, request: Request):
        f = _pfile(current(request), pid)
        if not f.exists():
            raise HTTPException(404, "Project not found")
        return json.loads(f.read_text())

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str, request: Request):
        f = _pfile(current(request), pid)
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
        detail = "Something went wrong on the server." if public else f"{type(exc).__name__}: {exc}"
        return JSONResponse({"detail": detail}, status_code=500)

    return app


def get_app() -> FastAPI:  # for `uvicorn datalens.api:get_app --factory`
    return create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*")
