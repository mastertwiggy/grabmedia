"""Configurazione centralizzata: tutte le variabili d'ambiente in un unico posto."""
import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# --- Redis -----------------------------------------------------------------
REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT: int = _int("REDIS_PORT", 6379)
REDIS_DB: int = _int("REDIS_DB", 0)
REDIS_PASSWORD: str | None = os.getenv("REDIS_PASSWORD") or None

if os.getenv("REDIS_URL"):
    REDIS_URL: str = os.environ["REDIS_URL"]
elif REDIS_PASSWORD:
    REDIS_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
else:
    REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

# --- Download --------------------------------------------------------------
DOWNLOAD_DIR: Path = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).resolve()
MAX_DOWNLOAD_SIZE_MB: int = _int("MAX_DOWNLOAD_SIZE_MB", 500)
MAX_DOWNLOAD_SIZE_BYTES: int = MAX_DOWNLOAD_SIZE_MB * 1024 * 1024

# Ore dopo le quali job e file vengono cancellati
FILE_TTL_HOURS: int = _int("FILE_TTL_HOURS", 24)
# Ogni quanti secondi il worker esegue la pulizia automatica
CLEANUP_INTERVAL_SECONDS: int = _int("CLEANUP_INTERVAL_SECONDS", 3600)

# --- Coda / worker ---------------------------------------------------------
QUEUE_NAME: str = os.getenv("QUEUE_NAME", "grabmedia")
JOB_TIMEOUT_SECONDS: int = _int("JOB_TIMEOUT_SECONDS", 1800)
PROBE_TIMEOUT_SECONDS: int = _int("PROBE_TIMEOUT_SECONDS", 120)

# --- API -------------------------------------------------------------------
JOBS_LIST_LIMIT: int = _int("JOBS_LIST_LIMIT", 10)
# Blocca URL che puntano a host privati/locali (protezione SSRF)
BLOCK_PRIVATE_HOSTS: bool = os.getenv("BLOCK_PRIVATE_HOSTS", "1") != "0"
