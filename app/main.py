"""App FastAPI di GrabMedia."""
import asyncio
import base64
import hashlib
import logging
import os
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
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

# --- Sessione a cookie per l'area "Provider audio" ---------------------------
# POST /login valida utente e password (nginx/.htpasswd o variabili d'ambiente) e
# rilascia un cookie HttpOnly, SameSite=Strict, a scadenza breve. Il cookie
# contiene solo un token casuale: le sessioni stanno in memoria nel processo
# (si azzerano al riavvio, con più worker ognuno ha le sue). Le credenziali non
# vengono mai salvate, né qui né nel browser.
SESSION_COOKIE = "grabmedia_session"
SESSION_TTL_SECONDS = int(os.environ.get("GRABMEDIA_SESSION_TTL", "1800"))  # 30 minuti
LOGIN_MAX_FAILURES = 5          # tentativi falliti per indirizzo...
LOGIN_WINDOW_SECONDS = 60.0     # ...in questa finestra, poi 429
PROTECTED_PREFIX = "/audio"     # /audio e tutto ciò che sta sotto, file compresi
_SESSIONS: dict[str, tuple[str, float]] = {}
_LOGIN_FAILURES: dict[str, list[float]] = {}
_AUTH_LOCK = threading.Lock()


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _AUTH_LOCK:
        for old, (_, expires) in list(_SESSIONS.items()):  # pulizia delle scadute
            if expires <= now:
                del _SESSIONS[old]
        _SESSIONS[token] = (username, now + SESSION_TTL_SECONDS)
    return token


def session_user(token: str | None) -> str | None:
    """Utente della sessione, ``None`` se il token manca, è sconosciuto o scaduto."""
    if not token:
        return None
    with _AUTH_LOCK:
        entry = _SESSIONS.get(token)
        if entry is None:
            return None
        username, expires = entry
        if expires <= time.time():
            del _SESSIONS[token]
            return None
        return username


def destroy_session(token: str | None) -> None:
    if token:
        with _AUTH_LOCK:
            _SESSIONS.pop(token, None)


def is_protected_path(path: str) -> bool:
    return path == PROTECTED_PREFIX or path.startswith(PROTECTED_PREFIX + "/")


def _login_retry_after(client: str) -> float | None:
    """Secondi da attendere se l'indirizzo ha esaurito i tentativi, altrimenti ``None``."""
    now = time.time()
    with _AUTH_LOCK:
        recent = [t for t in _LOGIN_FAILURES.get(client, []) if now - t < LOGIN_WINDOW_SECONDS]
        _LOGIN_FAILURES[client] = recent
        if len(recent) >= LOGIN_MAX_FAILURES:
            return LOGIN_WINDOW_SECONDS - (now - recent[0])
    return None


def _record_login_failure(client: str) -> None:
    with _AUTH_LOCK:
        _LOGIN_FAILURES.setdefault(client, []).append(time.time())


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


@app.middleware("http")
async def audio_session_guard(request: Request, call_next):
    """Protegge /audio e i download dei suoi file: senza un cookie di sessione
    valido risponde 401 (JSON), senza mai arrivare agli endpoint."""
    if is_protected_path(request.url.path) and session_user(request.cookies.get(SESSION_COOKIE)) is None:
        return JSONResponse(status_code=401, content={"detail": "Accesso richiesto", "login": "/login"})
    return await call_next(request)


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
        # no-cache: il browser ricontrolla sempre (ETag), così dopo un aggiornamento non resta la pagina vecchia
        return FileResponse(FRONTEND_INDEX, media_type="text/html", headers={"Cache-Control": "no-cache"})
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


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)


@app.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response):
    """Valida le credenziali e rilascia il cookie di sessione (HttpOnly, SameSite=Strict)."""
    client = request.client.host if request.client else "sconosciuto"
    wait = _login_retry_after(client)
    if wait is not None:
        raise HTTPException(status_code=429, detail="Troppi tentativi falliti: riprova tra poco",
                            headers={"Retry-After": str(int(wait) + 1)})
    ok = await run_in_threadpool(_audio_credentials_ok, payload.username, payload.password)
    if not ok:
        _record_login_failure(client)
        raise HTTPException(status_code=401, detail="Utente o password errati")
    token = create_session(payload.username)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, path="/",
        httponly=True, samesite="strict", secure=request.url.scheme == "https",
    )
    return {"authenticated": True, "user": payload.username, "expires_in": SESSION_TTL_SECONDS}


@app.post("/logout")
async def logout(request: Request, response: Response):
    destroy_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return {"authenticated": False}


@app.get("/session")
async def session_state(request: Request):
    """Stato della sessione per la pagina (il cookie è HttpOnly: il JS non lo può leggere)."""
    user = session_user(request.cookies.get(SESSION_COOKIE))
    return {"authenticated": user is not None, "user": user}


@app.post("/audio", status_code=202)
async def create_audio_task(request: AudioRequest):
    task = await run_in_threadpool(audio_queue().submit, request.url, request.format)
    log.info("Task audio %s accodato: %s [%s]", task.id, request.url, request.format or "config")
    return {"task_id": task.id, "state": task.state}


@app.post("/audio/cookies/renew")
async def renew_audio_cookies():
    """Rinnovo esplicito dei cookie letti dal browser (façade core.api)."""
    removed = await run_in_threadpool(renew_browser_cookies)
    return {"renewed": True, "removed_files": removed}


@app.get("/audio/{task_id}")
async def get_audio_task(task_id: str = PathParam(..., pattern=JOB_ID_PATTERN)):
    task = audio_queue().get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task non trovato")
    return _task_payload(task)


@app.get("/audio/{task_id}/file/{index}")
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
