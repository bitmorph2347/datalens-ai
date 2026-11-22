"""Per-visitor state for the public deployment.

Locally there is one global session (files in ``data/``). In public mode every browser gets its own session,
identified by a random id the page generates and sends in the ``X-Session-Id`` header. A session owns its
uploaded tables (a small in-memory DuckDB), saved projects, roster lists and price list, so visitors never see
each other's data. Sessions expire after a period of inactivity and the total number is capped.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pandas as pd

from .engine import Engine
from .rosters import RosterStore
from .uploads import UploadStore

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class SessionError(ValueError):
    pass


class Session:
    def __init__(self, engine: Engine, *, root: Path | None, projects_dir: Path, rosters_path: Path,
                 upload_mb: int, limited: bool):
        self.root = root
        self.projects_dir = projects_dir
        self.rosters = RosterStore(engine, rosters_path)
        self.prices: pd.DataFrame | None = None
        self.prices_source = "none"
        self.touched = time.time()
        self._upload_mb, self._limited = upload_mb, limited
        self._uploads: UploadStore | None = None
        self._lock = threading.Lock()

    @property
    def uploads(self) -> UploadStore:
        with self._lock:
            if self._uploads is None:  # created lazily: most visitors never upload anything
                self._uploads = UploadStore(max_mb=self._upload_mb, limited=self._limited,
                                            max_tables=3 if self._limited else 50)
            return self._uploads

    def close(self) -> None:
        if self._uploads is not None:
            try:
                self._uploads.con.close()
            except Exception:  # pragma: no cover - best effort
                pass
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)


class SessionManager:
    def __init__(self, engine: Engine, *, public: bool, projects_dir: Path, rosters_path: Path,
                 upload_mb: int | None = None, base_dir: Path | None = None, max_sessions: int = 300,
                 ttl_seconds: int = 6 * 3600):
        self.engine, self.public = engine, public
        self.max_sessions, self.ttl = max_sessions, ttl_seconds
        self.upload_mb = upload_mb or (25 if public else 200)
        self.base_dir = Path(base_dir or Path(tempfile.gettempdir()) / "datalens_sessions")
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = threading.Lock()
        self._global = None if public else Session(engine, root=None, projects_dir=projects_dir,
                                                   rosters_path=rosters_path, upload_mb=self.upload_mb, limited=False)

    def get(self, sid: str | None) -> Session:
        if not self.public:
            return self._global  # type: ignore[return-value]
        if not sid or not SESSION_ID_RE.match(sid):
            raise SessionError("Missing or invalid session id. Reload the page.")
        now = time.time()
        with self._lock:
            for k in [k for k, s in self._sessions.items() if now - s.touched > self.ttl]:
                self._sessions.pop(k).close()
            s = self._sessions.pop(sid, None)
            if s is None:
                root = self.base_dir / sid
                s = Session(self.engine, root=root, projects_dir=root / "projects", rosters_path=root / "rosters.csv",
                            upload_mb=self.upload_mb, limited=True)
            s.touched = now
            self._sessions[sid] = s  # most recently used goes last
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)[1].close()
            return s

    def count(self) -> int:
        return len(self._sessions)
