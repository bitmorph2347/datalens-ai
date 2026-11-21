from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "datalens.duckdb"
META_CSV = DATA_DIR / "players_meta.csv"
PRICES_CSV = DATA_DIR / "prices_sample.csv"
PROJECTS_DIR = DATA_DIR / "projects"
WEB_DIR = ROOT / "web"
CRICSHEET_URL = "https://cricsheet.org/downloads/ipl_male_csv2.zip"

# Phase boundaries (0-indexed overs): powerplay 0-5, middle 6-14, death 15-19
PHASES = {"powerplay": (0, 5), "middle": (6, 14), "death": (15, 19)}
