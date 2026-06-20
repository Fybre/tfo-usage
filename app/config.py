import os
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "./data/app.db")).resolve()
DATA_DIR = DB_PATH.parent
DEFAULT_EXPIRY_MONTHS = int(os.getenv("DEFAULT_EXPIRY_MONTHS", "6"))
IMMINENT_DAYS = int(os.getenv("IMMINENT_DAYS", "60"))
