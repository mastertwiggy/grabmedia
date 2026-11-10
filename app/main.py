"""App FastAPI di GrabMedia."""
import asyncio
import base64
import hashlib
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, field_validator
from rq import Queue
from starlette.concurrency import run_in_threadpool

from core.api import (  # unica superficie verso i provider audio (mai sources/)
    TaskQueue,
    js_runtime_available,
    js_runtime_missing_message,
    renew_browser_cookies,
)

from . import config, database, models

logging.basicConfig(
    level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("grabmedia.api")

JOB_ID_PATTERN = r"^[0-9a-f]{32}$"
FINAL_STATES = {models.JobStatus.COMPLETED.value, models.JobStatus.FAILED.value}
FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
WS_POLL_INTERVAL = 0.5


AUDIO_QUEUE: TaskQueue | None = None
AUDIO_FORMAT_PATTERN = r"^[A-Za-z0-9_:\-]{1,16}$"
HTPASSWD_PATH = Path(os.environ.get("GRABMEDIA_HTPASSWD") or Path(__file__).resolve().parent.parent / "nginx" / ".htpasswd")
_basic_auth = HTTPBasic(realm="GrabMedia", auto_error=False)


def _password_matches(stored: str, given: str) -> bool:
    """Confronta con una riga di .htpasswd: ``{SHA}base64(sha1)`` (formato di nginx) o testo in chiaro."""
    if stored.startswith("{SHA}"):
        digest = base64.b64encode(hashlib.sha1(given.encode("utf-8")).digest()).decode("ascii")
        return secrets.compare_digest(stored[5:], digest)
    return secrets.compare_digest(stored, given)


def _audio_credentials_ok(username: str, password: str) -> bool:
    env_user, env_pass = os.environ.get("GRABMEDIA_AUTH_USER"), os.environ.get("GRABMEDIA_AUTH_PASSWORD")
    if env_user and env_pass is not None:
        return secrets.compare_digest(username, env_user) and secrets.compare_digest(password, env_pass)
    try:
        lines = HTPASSWD_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        if ":" not in line or line.startswith("#"):
            continue
        user, stored = line.split(":", 1)
        if secrets.compare_digest(user.strip(), username) and _password_matches(stored.strip(), password):
            return True
    return False


def require_audio_auth(credentials: HTTPBasicCredentials | None = Depends(_basic_auth)) -> str:
    """Basic Auth sugli endpoint /audio stessi (non solo sui file via nginx).
    Credenziali: GRABMEDIA_AUTH_USER/GRABMEDIA_AUTH_PASSWORD oppure nginx/.htpasswd."""
    if credentials is None or not _audio_credentials_ok(credentials.username, credentials.password):
        raise HTTPException(status_code=401, detail="Autenticazione richiesta",
                            headers={"WWW-Authenticate": 'Basic realm="GrabMedia"'})
    return credentials.username


def audio_queue() -> TaskQueue:
    """Coda in-process dei provider audio (façade core.api), creata al primo uso.
    Senza runtime JavaScript (deno/node) l'app non va giù: avvisa e disabilita
    il provider YouTube, che senza runtime perderebbe formati."""
    global AUDIO_QUEUE
    if AUDIO_QUEUE is None:
        disabled: dict[str, str] = {}
        if not js_runtime_available():
            # il motivo completo (perché + le tre istruzioni di installazione) arriva fino all'utente
            disabled["youtube"] = js_runtime_missing_message()
            log.warning("Runtime JavaScript assente: provider youtube disabilitato.\n%s", disabled["youtube"])
        AUDIO_QUEUE = TaskQueue(disabled_providers=disabled)
    return AUDIO_QUEUE


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Download dir: %s | Redis: %s", config.DOWNLOAD_DIR, config.REDIS_URL)
    try:
        log.info("Provider audio: output in %s", audio_queue().cfg.out_dir)
    except Exception as exc:  # noqa: BLE001 - la web app resta su anche senza config.toml valido
        log.error("Coda provider audio non avviata: %s", exc)
    yield
    if AUDIO_QUEUE is not None:
        AUDIO_QUEUE.stop()


app = FastAPI(
    title="GrabMedia",
    description="Servizio di download video/audio basato su yt-dlp",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_queue() -> Queue:
    return Queue(config.QUEUE_NAME, connection=database.get_raw_redis())


@app.exception_handler(redis.RedisError)
async def redis_error_handler(_, exc: redis.RedisError):
    log.error("Errore Redis: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Servizio temporaneamente non disponibile (Redis)"},
    )


# --- Frontend / health -----------------------------------------------------
@app.get("/", include_in_schema=False)
async def index():
    if FRONTEND_INDEX.is_file():
        return FileResponse(FRONTEND_INDEX, media_type="text/html")
    raise HTTPException(status_code=404, detail="Frontend non trovato")


@app.get("/health", response_model=models.HealthResponse)
async def health():
    ok = await run_in_threadpool(database.ping)
    return models.HealthResponse(status="ok" if ok else "degraded", redis=ok)


# --- POST /download --------------------------------------------------------
@app.post("/download", response_model=models.DownloadResponse, status_code=202)
async def create_download(request: models.DownloadRequest):
    job_id = uuid.uuid4().hex
    fmt = request.format.value
    bitrate = request.audio_bitrate
    try:
        await run_in_threadpool(database.create_job, job_id, request.url, fmt, bitrate)
        await run_in_threadpool(
            lambda: get_queue().enqueue(
                "app.worker.process_download",
                job_id,
                request.url,
                fmt,
                bitrate,
                job_id=job_id,
                job_timeout=config.JOB_TIMEOUT_SECONDS + 60,
                result_ttl=config.FILE_TTL_HOURS * 3600,
                failure_ttl=config.FILE_TTL_HOURS * 3600,
            )
        )
    except redis.RedisError as exc:
        log.error("Redis non disponibile: %s", exc)
        raise HTTPException(status_code=503, detail="Servizio temporaneamente non disponibile (Redis)")
    log.info("Job %s accodato: %s [%s, %sk]", job_id, request.url, fmt, bitrate)
    return models.DownloadResponse(job_id=job_id)


# --- GET /status/{job_id} --------------------------------------------------
@app.get("/status/{job_id}", response_model=models.StatusResponse)
async def get_status(job_id: str = PathParam(..., pattern=JOB_ID_PATTERN)):
    record = await run_in_threadpool(database.get_job, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return models.status_from_record(record)


# --- WebSocket /ws/{job_id} ------------------------------------------------
@app.websocket("/ws/{job_id}")
async def ws_status(websocket: WebSocket, job_id: str):
    await websocket.accept()
    if not database.is_valid_job_id(job_id):
        await websocket.send_json({"job_id": job_id, "status": "failed", "progress": "0", "error_message": "Job id non valido"})
        await websocket.close(code=1008)
        return

    last_payload = None
    try:
        while True:
            record = await run_in_threadpool(database.get_job, job_id)
            if record is None:
                await websocket.send_json(
                    {"job_id": job_id, "status": "failed", "progress": "0", "error_message": "Job non trovato"}
                )
                break
            payload = models.status_from_record(record).model_dump()
            if payload != last_payload:
                await websocket.send_json(payload)
                last_payload = payload
            if record.get("status") in FINAL_STATES:
                break
            await asyncio.sleep(WS_POLL_INTERVAL)
    except WebSocketDisconnect:
        return
    except redis.RedisError as exc:
        log.error("WS %s: errore Redis %s", job_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


# --- GET /download/{job_id}/{filename} -------------------------------------
@app.get("/download/{job_id}/{filename}")
async def serve_file(filename: str, job_id: str = PathParam(..., pattern=JOB_ID_PATTERN)):
    record = await run_in_threadpool(database.get_job, job_id)
    if record is None or record.get("status") != models.JobStatus.COMPLETED.value:
        raise HTTPException(status_code=404, detail="File non disponibile")
    if not record.get("filename") or filename != record["filename"]:
        raise HTTPException(status_code=404, detail="File non trovato")

    base = config.DOWNLOAD_DIR.resolve()
    path = (base / job_id / record["filename"]).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Percorso non valido")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File non più presente (scaduto)")

    return FileResponse(path, filename=record["filename"], media_type="application/octet-stream")


# --- GET /jobs -------------------------------------------------------------
@app.get("/jobs", response_model=models.JobListResponse)
async def list_jobs():
    records = await run_in_threadpool(database.list_jobs, config.JOBS_LIST_LIMIT)
    return models.JobListResponse(jobs=[models.job_from_record(r) for r in records])


# --- POST /cleanup ---------------------------------------------------------
@app.post("/cleanup", response_model=models.CleanupResponse)
async def cleanup():
    result = await run_in_threadpool(database.cleanup_old_jobs, config.FILE_TTL_HOURS)
    return models.CleanupResponse(**result)


# --- Provider audio (façade core.api): /audio ------------------------------
class AudioRequest(BaseModel):
    url: str
    format: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return models.sanitize_url(value)

    @field_validator("format")
    @classmethod
    def _format(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        import re

        if not re.match(AUDIO_FORMAT_PATTERN, value):
            raise ValueError("formato non valido (es. best, mp3, mp3_320, opus, flac)")
        return value


def _task_payload(task) -> dict:
    data = task.to_dict()
    data["task_id"] = task.id
    data["download_urls"] = [f"/audio/{task.id}/file/{i}" for i in range(len(task.files))]
    data["filenames"] = [Path(f).name for f in task.files]
    return data


@app.post("/audio", status_code=202, dependencies=[Depends(require_audio_auth)])
async def create_audio_task(request: AudioRequest):
    task = await run_in_threadpool(audio_queue().submit, request.url, request.format)
    log.info("Task audio %s accodato: %s [%s]", task.id, request.url, request.format or "config")
    return {"task_id": task.id, "state": task.state}


@app.post("/audio/cookies/renew", dependencies=[Depends(require_audio_auth)])
async def renew_audio_cookies():
    """Rinnovo esplicito dei cookie letti dal browser (façade core.api)."""
    removed = await run_in_threadpool(renew_browser_cookies)
    return {"renewed": True, "removed_files": removed}


@app.get("/audio/{task_id}", dependencies=[Depends(require_audio_auth)])
async def get_audio_task(task_id: str = PathParam(..., pattern=JOB_ID_PATTERN)):
    task = audio_queue().get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task non trovato")
    return _task_payload(task)


@app.get("/audio/{task_id}/file/{index}", dependencies=[Depends(require_audio_auth)])
async def serve_audio_file(index: int, task_id: str = PathParam(..., pattern=JOB_ID_PATTERN)):
    queue = audio_queue()
    task = queue.get(task_id)
    if task is None or not task.finished or index < 0 or index >= len(task.files):
        raise HTTPException(status_code=404, detail="File non disponibile")
    base = Path(queue.cfg.out_dir).resolve()
    path = Path(task.files[index]).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Percorso non valido")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File non più presente")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")
