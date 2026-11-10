"""Modelli Pydantic e sanitizzazione URL."""
import re
from enum import Enum
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

from core.api import ApiError, check_public_url  # guard anti-SSRF unico (mai sources/)

from . import config


# --- Enumerazioni ----------------------------------------------------------
class DownloadFormat(str, Enum):
    BEST = "best"
    P1080 = "1080p"
    P720 = "720p"
    K4 = "4K"
    AUDIO = "solo audio (mp3)"


FORMAT_ALIASES: dict[str, DownloadFormat] = {
    "best": DownloadFormat.BEST,
    "migliore": DownloadFormat.BEST,
    "1080": DownloadFormat.P1080,
    "1080p": DownloadFormat.P1080,
    "720": DownloadFormat.P720,
    "720p": DownloadFormat.P720,
    "4k": DownloadFormat.K4,
    "2160": DownloadFormat.K4,
    "2160p": DownloadFormat.K4,
    "mp3": DownloadFormat.AUDIO,
    "audio": DownloadFormat.AUDIO,
    "solo audio": DownloadFormat.AUDIO,
    "solo audio (mp3)": DownloadFormat.AUDIO,
    "audio only": DownloadFormat.AUDIO,
}


MP3_BITRATES: tuple[int, ...] = (128, 192, 256, 320)
DEFAULT_MP3_BITRATE = 192


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# --- Sanitizzazione URL ----------------------------------------------------
MAX_URL_LENGTH = 2048
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")


class URLValidationError(ValueError):
    pass


def sanitize_url(raw: str) -> str:
    """Valida e normalizza un URL inviato dall'utente. Solleva URLValidationError."""
    if not isinstance(raw, str):
        raise URLValidationError("URL non valido")
    url = raw.strip()
    if not url:
        raise URLValidationError("URL mancante")
    if len(url) > MAX_URL_LENGTH:
        raise URLValidationError(f"URL troppo lungo (max {MAX_URL_LENGTH} caratteri)")
    if _CONTROL_RE.search(url):
        raise URLValidationError("L'URL contiene caratteri non validi")
    if url.startswith("-"):
        raise URLValidationError("URL non valido")
    if "://" not in url:
        url = "https://" + url

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise URLValidationError("Sono ammessi solo URL http e https")
    if parts.username or parts.password:
        raise URLValidationError("Credenziali nell'URL non consentite")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise URLValidationError("Host mancante nell'URL")
    try:
        port = parts.port
    except ValueError as exc:
        raise URLValidationError("Porta non valida nell'URL") from exc

    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise URLValidationError("Host non consentito")

    if config.BLOCK_PRIVATE_HOSTS:
        # Guard anti-SSRF unico del repo (range e risoluzione DNS in core.api):
        # l'app pretende anche che l'host sia risolvibile.
        try:
            check_public_url(url, require_resolvable=True)
        except ApiError as exc:
            raise URLValidationError(
                "Host non risolvibile" if exc.kind == "unresolvable" else "Host non consentito"
            ) from exc

    return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


# --- Richieste / risposte --------------------------------------------------
class DownloadRequest(BaseModel):
    url: str = Field(
        ...,
        description="URL del video/audio da scaricare",
        examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    )
    format: DownloadFormat = Field(
        DownloadFormat.BEST, description="best | 1080p | 720p | 4K | solo audio (mp3)"
    )
    audio_bitrate: int = Field(
        DEFAULT_MP3_BITRATE,
        description="Bitrate mp3 in kbps (128, 192, 256, 320); usato solo per 'solo audio (mp3)'",
    )

    @field_validator("audio_bitrate", mode="before")
    @classmethod
    def _validate_bitrate(cls, value):
        if value is None or value == "":
            return DEFAULT_MP3_BITRATE
        if isinstance(value, str):
            value = value.strip().lower().removesuffix("kbps").removesuffix("k").strip()
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Bitrate non valido: ammessi {', '.join(map(str, MP3_BITRATES))} kbps")
        if value not in MP3_BITRATES:
            raise ValueError(f"Bitrate non valido: ammessi {', '.join(map(str, MP3_BITRATES))} kbps")
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return sanitize_url(value)

    @field_validator("format", mode="before")
    @classmethod
    def _normalize_format(cls, value):
        if isinstance(value, str):
            key = value.strip().lower()
            if key in FORMAT_ALIASES:
                return FORMAT_ALIASES[key]
        return value


class DownloadResponse(BaseModel):
    job_id: str


class StatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: str = "0"
    error_message: Optional[str] = None
    filename: Optional[str] = None
    download_url: Optional[str] = None
    title: Optional[str] = None
    format: Optional[str] = None
    audio_bitrate: Optional[int] = None
    detail: Optional[str] = None


class Job(BaseModel):
    job_id: str
    url: str
    format: str
    audio_bitrate: Optional[int] = None
    status: JobStatus
    progress: str = "0"
    error_message: Optional[str] = None
    filename: Optional[str] = None
    download_url: Optional[str] = None
    title: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: float
    updated_at: float


class JobListResponse(BaseModel):
    jobs: list[Job]


class CleanupResponse(BaseModel):
    deleted_jobs: int
    deleted_files: int
    deleted_bytes: int


class HealthResponse(BaseModel):
    status: str
    redis: bool


# --- Conversioni da record Redis ------------------------------------------
def download_url_for(record: dict[str, str]) -> Optional[str]:
    if record.get("status") == JobStatus.COMPLETED.value and record.get("filename"):
        return f"/download/{record['job_id']}/{quote(record['filename'])}"
    return None


def _float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _bitrate(record: dict[str, str]) -> Optional[int]:
    """Bitrate mp3 solo se il job è (o è diventato) un download audio."""
    effective = record.get("effective_format") or record.get("format")
    value = record.get("audio_bitrate")
    if effective == DownloadFormat.AUDIO.value and value and value.isdigit():
        return int(value)
    return None


def status_from_record(record: dict[str, str]) -> StatusResponse:
    return StatusResponse(
        job_id=record["job_id"],
        status=record.get("status", "pending"),
        progress=record.get("progress", "0") or "0",
        error_message=record.get("error_message") or None,
        filename=record.get("filename") or None,
        download_url=download_url_for(record),
        title=record.get("title") or None,
        format=record.get("effective_format") or record.get("format") or None,
        audio_bitrate=_bitrate(record),
        detail=record.get("detail") or None,
    )


def job_from_record(record: dict[str, str]) -> Job:
    size = record.get("size_bytes")
    return Job(
        job_id=record["job_id"],
        url=record.get("url", ""),
        format=record.get("effective_format") or record.get("format", ""),
        audio_bitrate=_bitrate(record),
        status=record.get("status", "pending"),
        progress=record.get("progress", "0") or "0",
        error_message=record.get("error_message") or None,
        filename=record.get("filename") or None,
        download_url=download_url_for(record),
        title=record.get("title") or None,
        size_bytes=int(size) if size and size.isdigit() else None,
        created_at=_float(record.get("created_at")),
        updated_at=_float(record.get("updated_at")),
    )
