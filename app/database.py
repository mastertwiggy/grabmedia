"""Accesso a Redis: stato dei job, progresso, indice e pulizia."""
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

import redis

from . import config

log = logging.getLogger("grabmedia.database")

JOB_PREFIX = "grabmedia:job:"
JOBS_INDEX = "grabmedia:jobs"  # sorted set job_id -> created_at
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_redis: redis.Redis | None = None
_raw_redis: redis.Redis | None = None


# --- Connessioni -----------------------------------------------------------
def get_redis() -> redis.Redis:
    """Connessione con decode_responses=True per i dati applicativi."""
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(
            config.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
            health_check_interval=30,
        )
    return _redis


def get_raw_redis() -> redis.Redis:
    """Connessione 'grezza' (bytes) richiesta da RQ."""
    global _raw_redis
    if _raw_redis is None:
        _raw_redis = redis.Redis.from_url(
            config.REDIS_URL,
            decode_responses=False,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    return _raw_redis


def ping() -> bool:
    try:
        return bool(get_redis().ping())
    except redis.RedisError:
        return False


# --- Chiavi ----------------------------------------------------------------
def job_key(job_id: str) -> str:
    return f"{JOB_PREFIX}{job_id}"


def is_valid_job_id(job_id: str) -> bool:
    return bool(job_id) and bool(JOB_ID_RE.match(job_id))


# --- CRUD job --------------------------------------------------------------
def create_job(job_id: str, url: str, fmt: str, audio_bitrate: int | None = None) -> dict[str, str]:
    now = time.time()
    record = {
        "job_id": job_id,
        "url": url,
        "format": fmt,
        "effective_format": fmt,
        "audio_bitrate": str(audio_bitrate) if audio_bitrate else "",
        "status": "pending",
        "progress": "0",
        "error_message": "",
        "detail": "In coda",
        "title": "",
        "filename": "",
        "filepath": "",
        "size_bytes": "",
        "created_at": f"{now:.3f}",
        "updated_at": f"{now:.3f}",
    }
    pipe = get_redis().pipeline()
    pipe.hset(job_key(job_id), mapping=record)
    pipe.zadd(JOBS_INDEX, {job_id: now})
    pipe.execute()
    return record


def get_job(job_id: str) -> dict[str, str] | None:
    if not is_valid_job_id(job_id):
        return None
    data = get_redis().hgetall(job_key(job_id))
    return data or None


def update_job(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = f"{time.time():.3f}"
    mapping = {k: ("" if v is None else str(v)) for k, v in fields.items()}
    get_redis().hset(job_key(job_id), mapping=mapping)


def set_status(job_id: str, status: str, error_message: str | None = None, detail: str | None = None) -> None:
    fields: dict[str, Any] = {"status": status}
    if error_message is not None:
        fields["error_message"] = error_message[:1000]
    if detail is not None:
        fields["detail"] = detail
    elif status == "running":
        fields["detail"] = "Download in corso"
    elif status == "completed":
        fields["detail"] = "Completato"
    elif status == "failed":
        fields["detail"] = "Errore"
    update_job(job_id, **fields)


def get_status(job_id: str) -> str | None:
    return get_redis().hget(job_key(job_id), "status")


def set_progress(job_id: str, progress: float | int, detail: str | None = None) -> None:
    value = max(0, min(100, int(progress)))
    fields: dict[str, Any] = {"progress": str(value)}
    if detail is not None:
        fields["detail"] = detail
    update_job(job_id, **fields)


def get_progress(job_id: str) -> str:
    return get_redis().hget(job_key(job_id), "progress") or "0"


def set_result(job_id: str, filename: str, filepath: str, size_bytes: int) -> None:
    update_job(job_id, filename=filename, filepath=filepath, size_bytes=size_bytes)


def list_jobs(limit: int = 10) -> list[dict[str, str]]:
    r = get_redis()
    ids = r.zrevrange(JOBS_INDEX, 0, max(limit - 1, 0))
    if not ids:
        return []
    pipe = r.pipeline()
    for jid in ids:
        pipe.hgetall(job_key(jid))
    records = pipe.execute()
    jobs: list[dict[str, str]] = []
    stale: list[str] = []
    for jid, rec in zip(ids, records):
        if rec:
            jobs.append(rec)
        else:
            stale.append(jid)
    if stale:
        r.zrem(JOBS_INDEX, *stale)
    return jobs


# --- File / pulizia --------------------------------------------------------
def _remove_dir(path: Path) -> tuple[int, int]:
    """Rimuove una cartella e restituisce (numero_file, byte_liberati)."""
    files = 0
    size = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        log.warning("Impossibile rimuovere %s: %s", path, exc)
    return files, size


def remove_job_files(job_id: str) -> tuple[int, int]:
    if not is_valid_job_id(job_id):
        return 0, 0
    path = config.DOWNLOAD_DIR / job_id
    if path.is_dir():
        return _remove_dir(path)
    return 0, 0


def delete_job(job_id: str) -> bool:
    if not is_valid_job_id(job_id):
        return False
    remove_job_files(job_id)
    pipe = get_redis().pipeline()
    pipe.delete(job_key(job_id))
    pipe.zrem(JOBS_INDEX, job_id)
    deleted, _ = pipe.execute()
    return bool(deleted)


def cleanup_old_jobs(max_age_hours: float | None = None) -> dict[str, int]:
    """Cancella job (e relativi file) più vecchi di max_age_hours e cartelle orfane."""
    hours = config.FILE_TTL_HOURS if max_age_hours is None else max_age_hours
    cutoff = time.time() - hours * 3600
    r = get_redis()
    deleted_jobs = deleted_files = deleted_bytes = 0

    for jid in r.zrangebyscore(JOBS_INDEX, "-inf", cutoff):
        files, size = remove_job_files(jid)
        deleted_files += files
        deleted_bytes += size
        pipe = r.pipeline()
        pipe.delete(job_key(jid))
        pipe.zrem(JOBS_INDEX, jid)
        pipe.execute()
        deleted_jobs += 1

    # Cartelle orfane (job non più presenti in Redis) più vecchie del cutoff
    if config.DOWNLOAD_DIR.is_dir():
        for entry in config.DOWNLOAD_DIR.iterdir():
            if not entry.is_dir() or not is_valid_job_id(entry.name):
                continue
            if r.exists(job_key(entry.name)):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                files, size = _remove_dir(entry)
                deleted_files += files
                deleted_bytes += size

    if deleted_jobs or deleted_files:
        log.info("Pulizia: %d job, %d file, %d byte", deleted_jobs, deleted_files, deleted_bytes)
    return {"deleted_jobs": deleted_jobs, "deleted_files": deleted_files, "deleted_bytes": deleted_bytes}
