"""Logica di download basata su yt-dlp (eseguito in subprocess)."""
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from . import config, database

log = logging.getLogger("grabmedia.downloader")

AUDIO_FORMAT = "solo audio (mp3)"
MP3_BITRATES = (128, 192, 256, 320)
DEFAULT_MP3_BITRATE = 192

FORMAT_ARGS: dict[str, list[str]] = {
    "best": ["-f", "best/bestvideo+bestaudio"],
    "1080p": ["-f", "bestvideo[height<=1080]+bestaudio/best"],
    "720p": ["-f", "bestvideo[height<=720]+bestaudio/best"],
    "4K": ["-f", "bestvideo[height<=2160]+bestaudio/best"],
    AUDIO_FORMAT: ["-x", "--audio-format", "mp3"],
}


def format_args(fmt: str, audio_bitrate: int | None = None) -> list[str]:
    """Argomenti yt-dlp per il formato; per l'mp3 aggiunge il bitrate scelto."""
    args = list(FORMAT_ARGS[fmt])
    if fmt == AUDIO_FORMAT:
        bitrate = audio_bitrate if audio_bitrate in MP3_BITRATES else DEFAULT_MP3_BITRATE
        args += ["--audio-quality", f"{bitrate}K"]
    return args

# Siti che offrono solo audio: viene forzato il formato mp3
AUDIO_ONLY_DOMAINS: tuple[str, ...] = (
    "soundcloud.com",
    "snd.sc",
    "bandcamp.com",
    "mixcloud.com",
    "audiomack.com",
    "hearthis.at",
    "audioboom.com",
    "audius.co",
    "deezer.com",
    "podbean.com",
    "anchor.fm",
    "spreaker.com",
    "buzzsprout.com",
    "libsyn.com",
)

PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")
DESTINATION_RE = re.compile(r"\[download\]\s+Destination:")
POSTPROCESS_RE = re.compile(
    r"^\[(Merger|ExtractAudio|VideoConvertor|VideoRemuxer|Fixup\w*|Metadata|EmbedThumbnail|MoveFiles)\]"
)
TOO_LARGE_RE = re.compile(r"(larger than|exceeds).*max[- ]?filesize|File is larger than", re.IGNORECASE)
ERROR_RE = re.compile(r"^ERROR:\s*(.*)$")
TEMP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")


class DownloadError(Exception):
    """Errore 'pulito' da mostrare all'utente."""


# --- Utility ---------------------------------------------------------------
def ytdlp_command() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"]


def job_dir(job_id: str) -> Path:
    return config.DOWNLOAD_DIR / job_id


def cleanup_job_dir(job_id: str) -> None:
    path = job_dir(job_id)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def is_audio_only_site(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in AUDIO_ONLY_DOMAINS)


def _mb(size: int | float) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _has_video(info: dict) -> bool:
    formats = info.get("formats") or []
    if not formats:
        return info.get("vcodec") != "none"
    return any(f.get("vcodec") != "none" for f in formats)


def _estimated_size(info: dict) -> int | None:
    streams = info.get("requested_formats") or [info]
    total = 0
    for f in streams:
        size = f.get("filesize") or f.get("filesize_approx")
        if size:
            total += int(size)
    return total or None


def _find_output_file(directory: Path) -> Path | None:
    candidates = [
        p
        for p in directory.iterdir()
        if p.is_file() and not p.name.lower().endswith(TEMP_SUFFIXES)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
    return candidates[0]


def _extract_error(lines: deque[str]) -> str:
    for line in reversed(lines):
        m = ERROR_RE.match(line.strip())
        if m:
            return m.group(1).strip() or "Errore sconosciuto di yt-dlp"
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return "Errore sconosciuto di yt-dlp"


# --- Analisi preliminare ---------------------------------------------------
def probe(url: str, fmt_args: list[str]) -> dict | None:
    """Esegue yt-dlp -J per ottenere i metadati senza scaricare. None se fallisce."""
    cmd = ytdlp_command() + [
        "-J",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        "30",
        *fmt_args,
        "--",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("Probe fallito per %s: %s", url, exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        log.warning("Probe non riuscito (rc=%s): %s", proc.returncode, proc.stderr.strip()[-300:])
        return None
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    # Se è una playlist prendi la prima voce
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0] or {}
    return info


# --- Download --------------------------------------------------------------
def download_url(url: str, fmt: str, job_id: str, audio_bitrate: int | None = None) -> dict:
    """Scarica l'URL nel formato richiesto. Restituisce {filename, path, size, format}."""
    if fmt not in FORMAT_ARGS:
        raise DownloadError(f"Formato non supportato: {fmt}")
    if audio_bitrate is not None and audio_bitrate not in MP3_BITRATES:
        raise DownloadError(f"Bitrate mp3 non supportato: {audio_bitrate}")

    out_dir = job_dir(job_id)
    cleanup_job_dir(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    effective = fmt
    if fmt != AUDIO_FORMAT and is_audio_only_site(url):
        effective = AUDIO_FORMAT

    database.set_progress(job_id, 0, detail="Analisi URL in corso")
    info = probe(url, format_args(effective, audio_bitrate))
    n_streams = 1
    if info:
        if effective != AUDIO_FORMAT and not _has_video(info):
            effective = AUDIO_FORMAT
        est = _estimated_size(info)
        if est and est > config.MAX_DOWNLOAD_SIZE_BYTES:
            raise DownloadError(
                f"Dimensione stimata {_mb(est)} oltre il limite di {config.MAX_DOWNLOAD_SIZE_MB} MB"
            )
        n_streams = len(info.get("requested_formats") or []) or 1
        title = (info.get("title") or "")[:200]
        database.update_job(job_id, title=title, effective_format=effective)
    else:
        database.update_job(job_id, effective_format=effective)

    cmd = ytdlp_command() + [
        "--no-playlist",
        "--newline",
        "--progress",
        "--color",
        "no_color",
        "--restrict-filenames",
        "--no-cache-dir",
        "--no-mtime",
        "--max-filesize",
        f"{config.MAX_DOWNLOAD_SIZE_MB}m",
        "--socket-timeout",
        "30",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "-o",
        str(out_dir / "%(title).150B.%(ext)s"),
        *format_args(effective, audio_bitrate),
        "--",
        url,
    ]
    if effective == AUDIO_FORMAT:
        log.info("Job %s: avvio yt-dlp (%s, %sk)", job_id, effective, audio_bitrate or DEFAULT_MP3_BITRATE)
    else:
        log.info("Job %s: avvio yt-dlp (%s)", job_id, effective)
    database.set_progress(job_id, 0, detail="Download in corso")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(out_dir),
        )
    except OSError as exc:
        raise DownloadError(f"Impossibile avviare yt-dlp: {exc}") from exc

    tail: deque[str] = deque(maxlen=40)
    stream_idx = -1
    last_pct = -1
    too_large = False
    started = time.time()

    assert proc.stdout is not None
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            tail.append(line)

            if DESTINATION_RE.search(line):
                stream_idx += 1

            m = PROGRESS_RE.search(line)
            if m:
                pct = float(m.group(1))
                idx = max(stream_idx, 0)
                overall = int((idx * 100 + pct) / max(n_streams, 1))
                overall = max(0, min(overall, 99))
                if overall != last_pct:
                    last_pct = overall
                    database.set_progress(job_id, overall, detail="Download in corso")
            elif POSTPROCESS_RE.match(line):
                database.set_progress(job_id, 99, detail="Elaborazione file (ffmpeg)")

            if TOO_LARGE_RE.search(line):
                too_large = True

            if time.time() - started > config.JOB_TIMEOUT_SECONDS:
                proc.kill()
                raise DownloadError(
                    f"Timeout: il download ha superato {config.JOB_TIMEOUT_SECONDS // 60} minuti"
                )
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        returncode = proc.wait()

    if too_large:
        raise DownloadError(f"Il file supera il limite di {config.MAX_DOWNLOAD_SIZE_MB} MB")
    if returncode != 0:
        raise DownloadError(_extract_error(tail))

    output = _find_output_file(out_dir)
    if output is None:
        raise DownloadError("yt-dlp non ha prodotto alcun file")

    size = output.stat().st_size
    if size > config.MAX_DOWNLOAD_SIZE_BYTES:
        output.unlink(missing_ok=True)
        raise DownloadError(
            f"Il file scaricato ({_mb(size)}) supera il limite di {config.MAX_DOWNLOAD_SIZE_MB} MB"
        )

    # Rimuove eventuali residui temporanei
    for p in out_dir.iterdir():
        if p.is_file() and p != output:
            p.unlink(missing_ok=True)

    log.info("Job %s completato: %s (%s)", job_id, output.name, _mb(size))
    return {"filename": output.name, "path": str(output), "size": size, "format": effective}
