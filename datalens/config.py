from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "datalens.duckdb"
META_CSV = DATA_DIR / "players_meta.csv"
PRICES_CSV = DATA_DIR / "prices_sample.csv"
PROJECTS_DIR = DATA_DIR / "projects"
WEB_DIR = ROOT / "web"
CRICSHEET_BASE = "https://cricsheet.org/downloads"

# Phase boundaries (0-indexed overs) per format
PHASES = {
    "T20": {"powerplay": (0, 5), "middle": (6, 14), "death": (15, 19)},
    "ODI": {"powerplay": (0, 9), "middle": (10, 39), "death": (40, 49)},
}
