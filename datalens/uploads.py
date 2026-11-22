"""User-uploaded datasets: profiling, cleaning and read-only SQL exploration."""
from __future__ import annotations

import io
import os
import re
import threading

import duckdb
import numpy as np
import pandas as pd

from .chat import safe_select
from .engine import records

MAX_MB = int(os.environ.get("DATALENS_MAX_UPLOAD_MB", "200"))  # raise/lower with the env var
MAX_BYTES = MAX_MB * 1024 * 1024
MAX_ROWS = 5_000_000


def _slug(name: str) -> str:
    stem = re.sub(r"\.[^.]+$", "", name)
    s = re.sub(r"\W+", "_", stem).strip("_").lower() or "data"
    return "u_" + (s if not s[0].isdigit() else "_" + s)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


class UploadStore:
    def __init__(self, max_mb: int = MAX_MB, limited: bool = False, max_tables: int = 50) -> None:
        self.max_mb, self.max_bytes, self.max_tables = max_mb, max_mb * 1024 * 1024, max_tables
        self.con = duckdb.connect(":memory:")
        self.con.execute("SET enable_external_access=false")
        if limited:  # public site: keep one visitor's queries from starving the others
            self.con.execute("SET threads=1")
            self.con.execute("SET memory_limit='400MB'")
        self.frames: dict[str, pd.DataFrame] = {}
        self.lock = threading.Lock()

    def _load(self, name: str, df: pd.DataFrame) -> None:
        self.frames[name] = df
        self.con.register("_tmp_df", df)
        self.con.execute(f"CREATE OR REPLACE TABLE {_q(name)} AS SELECT * FROM _tmp_df")
        self.con.unregister("_tmp_df")

    def add(self, filename: str, content: bytes) -> dict:
        if len(content) > self.max_bytes:
            raise ValueError(f"File too large (max {self.max_mb} MB).")
        if not filename.lower().endswith((".csv", ".tsv", ".txt")):
            raise ValueError("Please upload a .csv or .tsv file")
        sep = "\t" if filename.lower().endswith(".tsv") else ","
        df = None
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                try:  # fast C parser first; fall back to delimiter sniffing for odd files
                    df = pd.read_csv(io.BytesIO(content), sep=sep, encoding=enc, nrows=MAX_ROWS)
                except pd.errors.ParserError:
                    df = pd.read_csv(io.BytesIO(content), sep=None, engine="python", encoding=enc, nrows=MAX_ROWS)
                break
            except UnicodeDecodeError:
                continue
            except Exception as exc:
                raise ValueError(f"Could not read the file as CSV: {exc}") from None
        if df is None or df.empty:
            raise ValueError("The file has no rows")
        df.columns = [re.sub(r"\s+", "_", str(c).strip()) or f"col_{i}" for i, c in enumerate(df.columns)]
        name = _slug(filename)
        if name not in self.frames and len(self.frames) >= self.max_tables:
            raise ValueError(f"You can keep at most {self.max_tables} uploaded tables at once.")
        with self.lock:
            self._load(name, df)
        return {"table": name, **self.profile(name)}

    def names(self) -> list[str]:
        return list(self.frames)

    def _get(self, name: str) -> pd.DataFrame:
        if name not in self.frames:
            raise ValueError(f"No uploaded table '{name}'")
        return self.frames[name]

    def profile(self, name: str) -> dict:
        df = self._get(name)
        cols, issues = [], []
        for c in df.columns:
            s = df[c]
            miss = int(s.isna().sum())
            info = {"name": c, "dtype": str(s.dtype), "missing": miss, "missing_pct": round(miss * 100 / len(df), 1),
                    "unique": int(s.nunique(dropna=True))}
            if pd.api.types.is_numeric_dtype(s):
                nn = s.dropna()
                if len(nn):
                    info.update(min=float(nn.min()), max=float(nn.max()), mean=round(float(nn.mean()), 3))
                    q1, q3 = nn.quantile(0.25), nn.quantile(0.75)
                    iqr = q3 - q1
                    out = int(((nn < q1 - 3 * iqr) | (nn > q3 + 3 * iqr)).sum()) if iqr > 0 else 0
                    info["outliers"] = out
                    if out:
                        issues.append(f"'{c}': {out} extreme outlier value(s)")
            else:
                info["sample"] = [str(v) for v in s.dropna().unique()[:3]]
                non_null = s.dropna().astype(str)
                if len(non_null) and pd.to_numeric(non_null.str.replace(",", ""), errors="coerce").notna().mean() > 0.9:
                    issues.append(f"'{c}': looks numeric but is stored as text")
                if len(non_null) and (non_null != non_null.str.strip()).any():
                    issues.append(f"'{c}': has leading/trailing whitespace")
                low = non_null.str.strip().str.lower().nunique()
                if low < non_null.str.strip().nunique():
                    issues.append(f"'{c}': inconsistent capitalisation (e.g. 'India' vs 'india')")
            if info["missing_pct"] >= 30:
                issues.append(f"'{c}': {info['missing_pct']}% missing")
            if info["unique"] <= 1 and len(df) > 1:
                issues.append(f"'{c}': constant column")
            cols.append(info)
        dups = int(df.duplicated().sum())
        if dups:
            issues.append(f"{dups} duplicate row(s)")
        total_missing = int(df.isna().sum().sum())
        score = max(0, 100 - min(40, total_missing * 100 // max(1, df.size) * 2) - min(20, dups * 100 // max(1, len(df))) - 3 * len(issues))
        return {"rows": len(df), "columns": cols, "duplicates": dups, "missing_cells": total_missing,
                "quality_score": int(score), "issues": issues, "preview": records(df.head(10))}

    def clean(self, name: str, fill: str = "none") -> dict:
        df = self._get(name).copy()
        before, actions = len(df), []
        for c in [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]:
            stripped = df[c].astype("string").str.strip()
            if not stripped.equals(df[c].astype("string")):
                df[c] = stripped
                actions.append(f"trimmed whitespace in '{c}'")
            num = pd.to_numeric(df[c].astype("string").str.replace(",", "", regex=False), errors="coerce")
            if df[c].notna().any() and num.notna().sum() >= 0.9 * df[c].notna().sum():
                df[c] = num
                actions.append(f"converted '{c}' to numbers")
        empty = [c for c in df.columns if df[c].isna().all()]
        if empty:
            df = df.drop(columns=empty)
            actions.append(f"dropped empty column(s): {', '.join(empty)}")
        d = int(df.duplicated().sum())
        if d:
            df = df.drop_duplicates()
            actions.append(f"removed {d} duplicate row(s)")
        if fill == "median":
            for c in df.select_dtypes(include=[np.number]).columns:
                n = int(df[c].isna().sum())
                if n:
                    df[c] = df[c].fillna(df[c].median())
                    actions.append(f"filled {n} missing value(s) in '{c}' with the median")
        with self.lock:
            self._load(name, df)
        return {"table": name, "actions": actions or ["nothing needed cleaning"], "rows_before": before,
                "rows_after": len(df), **self.profile(name)}

    def query(self, sql: str) -> dict:
        s = safe_select(sql)
        with self.lock:
            try:
                df = self.con.execute(s).df()
            except duckdb.Error as exc:
                raise ValueError(f"SQL error: {exc}") from None
        return {"columns": list(df.columns), "rows": records(df), "sql": sql}

    def chart(self, name: str, x: str, y: str | None = None, agg: str = "count", limit: int = 30) -> dict:
        df = self._get(name)
        if x not in df.columns or (y and y not in df.columns):
            raise ValueError("Unknown column")
        if agg not in ("count", "sum", "avg", "min", "max", "median"):
            raise ValueError("Unsupported aggregate")
        if y is None or agg == "count":
            sql = f"SELECT {_q(x)} AS label, count(*) AS value FROM {_q(name)} GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}"
        else:
            sql = f"SELECT {_q(x)} AS label, {agg}({_q(y)}) AS value FROM {_q(name)} GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}"
        with self.lock:
            out = self.con.execute(sql).df()
        return {"labels": [str(v) for v in out["label"]], "values": [None if pd.isna(v) else float(v) for v in out["value"]]}
