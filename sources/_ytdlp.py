"""Helper condiviso dai provider basati su yt-dlp (youtube, generic, soundcloud, bandcamp).

Il prefisso ``_`` lo esclude dal registry. Qui vivono:

* verifica di yt-dlp e ffmpeg (``ProviderUnavailable`` con hint);
* parsing della qualità e scelta del formato: ``best`` = formato nativo senza
  transcodifica; un codec esplicito preferisce lo stream nativo in quel codec
  (``bestaudio[acodec^=mp3]``) e yt-dlp copia senza ricodificare quando il
  codec sorgente coincide (quindi ``mp3_320`` su una sorgente mp3-128 resta
  128: non si inventa qualità);
* classificazione degli errori di yt-dlp con una tabella esplicita di pattern
  (``ERROR_RULES``); un errore non riconosciuto è ritentato UNA volta e poi è
  permanente, e il messaggio grezzo viene sempre loggato così la tabella può
  crescere dai casi reali;
* ``safe_extract``: ``extract_info`` che non lascia passare ``None``
  (``ignoreerrors``) né generatori (``process=False``);
* ``download_audio``: download su cartella temporanea, riuso opzionale di un
  ``info`` già risolto (``process_ie_result``) per non ri-estrarre, spostamento
  sul percorso finale.
"""

from __future__ import annotations

import atexit
import copy
import logging
import os
import re
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core import stats
from sources.base import NeedsCookies, ProgressCallback, ProviderError, ProviderUnavailable, Track

try:  # yt-dlp è opzionale: senza, i provider che lo usano sono "non disponibili"
    import yt_dlp
    from yt_dlp.utils import DownloadError, ExtractorError
except ImportError:  # pragma: no cover - dipende dall'ambiente
    yt_dlp = None  # type: ignore[assignment]

    class DownloadError(Exception):  # type: ignore[no-redef]
        pass

    class ExtractorError(Exception):  # type: ignore[no-redef]
        pass

log = logging.getLogger("grabmedia.ytdlp")

AUDIO_EXTS = {"m4a", "mp3", "opus", "ogg", "oga", "flac", "wav", "aac", "webm", "mp4", "mka"}
TEMP_SUFFIXES = (".part", ".ytdl", ".tmp", ".temp")
HEAVY_INFO_KEYS = ("automatic_captions", "subtitles", "heatmap", "comments", "requested_formats",
                   "requested_downloads", "__postprocessors", "_format_sort_fields")


# ------------------------------------------------------------------ requisiti
def require_ytdlp(provider: str) -> Any:
    if yt_dlp is None:
        raise ProviderUnavailable(
            "il pacchetto yt-dlp non è installato",
            hint="Esegui: pip install -U yt-dlp",
            provider=provider,
        )
    return yt_dlp


def _ffmpeg_ok(location: str) -> bool:
    p = Path(location)
    if p.is_file():
        return True
    if p.is_dir():
        return any((p / n).is_file() for n in ("ffmpeg", "ffmpeg.exe"))
    return False


def find_ffmpeg(options: Mapping[str, Any] | None, provider: str) -> str | None:
    """Percorso di ffmpeg da ``options.ffmpeg_location``, ``$FFMPEG_LOCATION``
    o dal PATH. Solleva ``ProviderUnavailable`` se non c'è."""
    options = options or {}
    configured = options.get("ffmpeg_location") or os.environ.get("FFMPEG_LOCATION")
    if configured:
        if _ffmpeg_ok(str(configured)):
            return str(configured)
        raise ProviderUnavailable(
            f"ffmpeg non trovato in '{configured}'",
            hint="Correggi ffmpeg_location in [options.<provider>] di config.toml "
            "(file ffmpeg.exe o cartella che lo contiene).",
            provider=provider,
        )
    found = shutil.which("ffmpeg")
    if found:
        return None  # nel PATH: yt-dlp lo trova da solo
    raise ProviderUnavailable(
        "ffmpeg non è nel PATH",
        hint="Installa ffmpeg (Windows: winget install Gyan.FFmpeg; Debian/Ubuntu: apt install ffmpeg; "
        "macOS: brew install ffmpeg) oppure imposta ffmpeg_location in [options.<provider>] di config.toml.",
        provider=provider,
    )


# -------------------------------------------------------------------- qualità
CODECS = {
    "best": "best", "native": "best",
    "m4a": "m4a", "aac": "m4a", "alac": "alac",
    "mp3": "mp3", "opus": "opus", "vorbis": "vorbis", "ogg": "vorbis",
    "flac": "flac", "wav": "wav",
}
CODEC_EXT: dict[str, str | None] = {
    "best": None, "m4a": "m4a", "alac": "m4a", "mp3": "mp3", "opus": "opus",
    "vorbis": "ogg", "flac": "flac", "wav": "wav",
}
# acodec riportato da yt-dlp per ogni codec: serve a preferire lo stream nativo.
CODEC_ACODEC = {"m4a": "aac", "alac": "alac", "mp3": "mp3", "opus": "opus", "vorbis": "vorbis", "flac": "flac", "wav": "pcm"}
BITRATE_CODECS = ("mp3", "opus", "vorbis", "m4a")
_QUALITY_RE = re.compile(r"^\s*([a-z][a-z0-9]*)?\s*[:_ -]?\s*(\d{2,3})?\s*k?\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Quality:
    codec: str
    bitrate: int | None = None

    @property
    def ext(self) -> str | None:
        """Estensione attesa; ``None`` per ``best`` (dipende dalla sorgente)."""
        return CODEC_EXT[self.codec]

    @property
    def native(self) -> bool:
        return self.codec == "best"

    def format_selector(self) -> str:
        if self.native:
            return "bestaudio/best"
        return f"bestaudio[acodec^={CODEC_ACODEC[self.codec]}]/bestaudio/best"


def parse_quality(quality: str | None, provider: str) -> Quality:
    """``best`` -> formato nativo (nessuna transcodifica); ``m4a``, ``mp3``,
    ``mp3_320``, ``320k``, ``opus``, ``vorbis``, ``flac``, ``wav``, ``alac``."""
    raw = (quality or "best").strip().lower()
    m = _QUALITY_RE.match(raw)
    if not m or (m.group(1) is None and m.group(2) is None):
        raise ProviderError(
            f"qualità non riconosciuta: '{quality}'",
            hint=f"Valori validi: {', '.join(sorted(CODECS))}, oppure mp3_320 / 320k.",
            provider=provider,
        )
    codec_key = m.group(1) or "mp3"
    if codec_key not in CODECS:
        raise ProviderError(
            f"codec non supportato: '{codec_key}'",
            hint=f"Valori validi: {', '.join(sorted(CODECS))}.",
            provider=provider,
        )
    bitrate = int(m.group(2)) if m.group(2) else None
    codec = CODECS[codec_key]
    if bitrate is not None and codec not in BITRATE_CODECS:
        bitrate = None
    return Quality(codec, bitrate)


# -------------------------------------------------------------------- opzioni
class ExtractLogger:
    """Logger per yt-dlp, UNO PER ESTRAZIONE: raccoglie gli errori della sola
    chiamata a cui appartiene (``last_error``), così con estrazioni concorrenti
    nessuna traccia eredita l'errore di un'altra. Non c'è stato di modulo per
    gli errori; l'unico stato condiviso è la deduplica dei warning (yt-dlp
    ripete lo stesso avviso a ogni estrazione, es. quello sul runtime JS)."""

    _seen_warnings: set[str] = set()

    def __init__(self) -> None:
        self._log = logging.getLogger("grabmedia.ytdlp.lib")
        self.last_error: str | None = None
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        self._log.debug(msg[8:] if msg.startswith("[debug] ") else msg)

    def info(self, msg: str) -> None:
        self._log.info(msg)

    def warning(self, msg: str) -> None:
        key = msg.strip()
        if key in self._seen_warnings:
            return
        self._seen_warnings.add(key)
        self._log.warning(msg)

    def error(self, msg: str) -> None:
        self.last_error = msg
        self.errors.append(msg)
        self._log.error(msg)


def base_opts(
    options: Mapping[str, Any] | None,
    *,
    extract_flat: bool = False,
    noplaylist: bool = False,
    ignoreerrors: bool = False,
) -> dict:
    options = options or {}
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "ignoreerrors": ignoreerrors,
        "noplaylist": noplaylist,
        "logger": ExtractLogger(),
    }
    if extract_flat:
        opts["extract_flat"] = "in_playlist"
    cookiefile = effective_cookiefile(options)
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if options.get("ffmpeg_location"):
        opts["ffmpeg_location"] = str(options["ffmpeg_location"])
    elif os.environ.get("FFMPEG_LOCATION"):
        opts["ffmpeg_location"] = os.environ["FFMPEG_LOCATION"]
    runtime = js_runtime_param(os.environ.get("YTDLP_JS_RUNTIME"))
    if runtime:
        opts["js_runtimes"] = runtime
    return opts


# ------------------------------------------------ cookie del browser: ciclo di vita
# Directory dedicata (override: GRABMEDIA_COOKIE_DIR), non file sparsi in /tmp.
COOKIE_DIR_ENV = "GRABMEDIA_COOKIE_DIR"
COOKIE_FILE_PREFIX = "grabmedia-cookies-"
# Nome completo: grabmedia-cookies-<pid>-<random>.txt. Lo sweep tocca SOLO i
# file che rispettano questo pattern, qualunque sia la directory configurata.
COOKIE_FILE_RE = re.compile(rf"^{re.escape(COOKIE_FILE_PREFIX)}(\d+)-[A-Za-z0-9_]+\.txt$")
COOKIE_TTL_SECONDS = 6 * 3600          # dopo 6 ore il file è stantio: la prossima necessità lo ri-risolve
COOKIE_ORPHAN_MAX_AGE_SECONDS = 86400  # sweep all'avvio: file orfani più vecchi di un giorno
_BROWSER_COOKIE_FILES: dict[str, str] = {}
_BROWSER_COOKIE_LOCK = threading.Lock()
_SIGNALS_INSTALLED = False


def cookie_file_pid(path: Path | str) -> int | None:
    """pid codificato nel nome del file cookie, ``None`` se il nome non è nostro."""
    m = COOKIE_FILE_RE.match(Path(path).name)
    return int(m.group(1)) if m else None


def pid_alive(pid: int) -> bool:
    """``True`` se il processo esiste ancora. POSIX: ``os.kill(pid, 0)``.
    Windows: ``os.kill(pid, 0)`` TERMINEREBBE il processo, quindi si interroga
    il processo con ``OpenProcess``/``GetExitCodeProcess``. In dubbio (permessi
    negati) si considera vivo: meglio un orfano in più che un file strappato
    a un processo attivo."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: esiste ma non è nostro
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def cookie_dir() -> Path:
    base = os.environ.get(COOKIE_DIR_ENV) or os.path.join(tempfile.gettempdir(), "grabmedia", "cookies")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _extract_browser_cookies(spec: tuple[str, str | None, str | None, str | None]):
    """Legge i cookie dal browser tramite yt-dlp (sostituibile nei test)."""
    from yt_dlp.cookies import extract_cookies_from_browser

    browser, profile, keyring, container = spec
    return extract_cookies_from_browser(browser, profile, keyring=keyring, container=container)


def sweep_cookie_dir(
    max_age_seconds: float = COOKIE_ORPHAN_MAX_AGE_SECONDS,
    *,
    now: float | None = None,
    alive: Any = None,
) -> int:
    """All'avvio: cancella dalla directory dedicata i file cookie orfani.

    Tocca ESCLUSIVAMENTE i file che rispettano il pattern completo
    ``grabmedia-cookies-<pid>-<random>.txt`` (anche con ``GRABMEDIA_COOKIE_DIR``
    puntata a una directory con altri file), salta quelli il cui pid è ancora
    vivo e cancella solo i morti più vecchi di ``max_age_seconds``.
    ``alive`` sostituisce ``pid_alive`` (test). Ritorna quanti file ha rimosso."""
    now = time.time() if now is None else now
    alive = alive or pid_alive
    removed = 0
    try:
        entries = list(cookie_dir().iterdir())
    except OSError:
        return 0
    with _BROWSER_COOKIE_LOCK:
        mine = set(_BROWSER_COOKIE_FILES.values())
    for entry in entries:
        pid = cookie_file_pid(entry)
        if pid is None or str(entry) in mine or not entry.is_file():
            continue
        if alive(pid):
            continue
        try:
            if now - entry.stat().st_mtime > max_age_seconds:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("sweep cookie: rimossi %d file orfani da %s", removed, cookie_dir())
    return removed


def _cleanup_browser_cookie_files() -> None:
    """Cancella i file cookie di QUESTO processo (atexit, SIGTERM, SIGINT,
    rinnovo): solo quelli registrati dalla sessione e con il nostro pid nel nome."""
    me = os.getpid()
    with _BROWSER_COOKIE_LOCK:
        for path in _BROWSER_COOKIE_FILES.values():
            if cookie_file_pid(path) != me:
                continue
            try:
                os.remove(path)
            except OSError:
                pass
        _BROWSER_COOKIE_FILES.clear()


def renew_browser_cookies() -> int:
    """Rinnovo esplicito: butta i file di sessione; la prossima necessità
    rilegge il browser. Ritorna quanti file sono stati rimossi."""
    with _BROWSER_COOKIE_LOCK:
        count = len(_BROWSER_COOKIE_FILES)
    _cleanup_browser_cookie_files()
    return count


def install_signal_cleanup() -> bool:
    """Pulizia anche su SIGTERM e SIGINT (non solo atexit): l'handler cancella i
    file e poi passa la mano all'handler precedente, o ripristina il default e
    rilancia il segnale. Solo nel thread principale; ritorna ``True`` se installato."""
    global _SIGNALS_INSTALLED
    if _SIGNALS_INSTALLED:
        return True
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous = signal.getsignal(signum)

            def handler(received, frame, _previous=previous, _signum=signum):  # noqa: ANN001
                _cleanup_browser_cookie_files()
                if callable(_previous) and _previous not in (signal.SIG_DFL, signal.SIG_IGN):
                    _previous(received, frame)
                    return
                if _previous == signal.SIG_IGN:
                    return
                signal.signal(_signum, signal.SIG_DFL)
                signal.raise_signal(_signum)

            signal.signal(signum, handler)
    except (ValueError, OSError, AttributeError) as exc:  # non nel thread principale, o piattaforma senza il segnale
        log.debug("handler dei segnali per i cookie non installato: %s", exc)
        return False
    _SIGNALS_INSTALLED = True
    return True


def _reset_signal_cleanup_for_tests() -> None:
    global _SIGNALS_INSTALLED
    _SIGNALS_INSTALLED = False


atexit.register(_cleanup_browser_cookie_files)


def browser_cookie_file(spec_text: str, *, now: float | None = None) -> str:
    """Risolve ``cookiesfrombrowser`` una volta per sessione: i cookie vengono
    letti dal browser, scritti in un file Netscape nella directory dedicata
    (permessi 600, cancellato all'uscita, a SIGTERM/SIGINT o al rinnovo) e quel
    file viene riusato da ogni YoutubeDL e dal percorso urllib di Bandcamp.
    Nessuna rilettura finché il file esiste e ha meno di ``COOKIE_TTL_SECONDS``."""
    key = spec_text.strip()
    now = time.time() if now is None else now
    with _BROWSER_COOKIE_LOCK:
        cached = _BROWSER_COOKIE_FILES.get(key)
        if cached and os.path.isfile(cached):
            try:
                age = now - os.stat(cached).st_mtime
            except OSError:
                age = float("inf")
            if age <= COOKIE_TTL_SECONDS:
                return cached
            log.info("cookie del browser stantii (%.0f min): li ri-risolvo", age / 60)
            try:
                os.remove(cached)
            except OSError:
                pass
            _BROWSER_COOKIE_FILES.pop(key, None)
        spec = parse_browser_spec(key)
        try:
            jar = _extract_browser_cookies(spec)
        except Exception as exc:  # noqa: BLE001 - yt-dlp solleva tipi diversi (ValueError, FileNotFoundError, ...)
            raise ProviderError(
                f"impossibile leggere i cookie dal browser '{spec[0]}': {exc}",
                hint="Chiudi il browser o indica il profilo (es. firefox:default); in alternativa esporta i cookie "
                "in un file Netscape e usa --cookies.", kind="usage",
            ) from None
        # nome con il pid: grabmedia-cookies-<pid>-<random>.txt (vedi COOKIE_FILE_RE)
        fd, path = tempfile.mkstemp(prefix=f"{COOKIE_FILE_PREFIX}{os.getpid()}-", suffix=".txt", dir=str(cookie_dir()))
        os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        jar.save(path, ignore_discard=True, ignore_expires=True)
        _BROWSER_COOKIE_FILES[key] = path
        log.info("cookie del browser '%s' risolti una volta per questa sessione (%s)", spec[0], path)
        return path


def effective_cookiefile(options: Mapping[str, Any] | None) -> str | None:
    """Il file di cookie da usare: ``cookiefile`` se presente, altrimenti il file
    temporaneo ricavato da ``cookiesfrombrowser`` (risolto una volta)."""
    options = options or {}
    if options.get("cookiefile"):
        return str(options["cookiefile"])
    if options.get("cookiesfrombrowser"):
        return browser_cookie_file(str(options["cookiesfrombrowser"]))
    return None


def parse_browser_spec(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """``browser[+keyring][:profilo][::container]`` (sintassi di --cookies-from-browser)
    -> tupla ``(browser, profile, keyring, container)`` attesa dal parametro
    ``cookiesfrombrowser`` di yt-dlp."""
    spec = spec.strip()
    container = None
    if "::" in spec:
        spec, container = spec.split("::", 1)
    browser, profile = (spec.split(":", 1) + [None])[:2] if ":" in spec else (spec, None)
    keyring = None
    if "+" in browser:
        browser, keyring = browser.split("+", 1)
    browser = browser.strip().lower()
    if not browser:
        raise ProviderError("--cookies-from-browser: nome del browser mancante",
                            hint="Esempi: chrome, firefox:default, chrome+gnomekeyring:Profile 1")
    return (browser, (profile or None), (keyring.upper() if keyring else None), (container or None))


def js_runtime_param(value: str | None) -> dict[str, dict[str, str]] | None:
    """``YTDLP_JS_RUNTIME`` (``deno`` o ``deno:/percorso``, esportata da
    ``core.jsruntime``) -> parametro ``js_runtimes`` esplicito per yt-dlp."""
    if not value or not value.strip():
        return None
    value = value.strip()
    if ":" in value and not (len(value) > 1 and value[1] == ":" and value[0].isalpha()):
        name, path = value.split(":", 1)
        return {name.strip().lower(): ({"path": path.strip()} if path.strip() else {})}
    return {value.lower(): {}}


def make_ydl(
    options: Mapping[str, Any] | None,
    *,
    extract_flat: bool = False,
    noplaylist: bool = False,
    ignoreerrors: bool = False,
):
    return yt_dlp.YoutubeDL(base_opts(options, extract_flat=extract_flat, noplaylist=noplaylist, ignoreerrors=ignoreerrors))


# --------------------------------------------------------------------- errori
COOKIES_HINT = (
    "need_cookies: il sito chiede un login o una verifica anti-bot. Passa i cookie a yt-dlp con "
    "--cookies <file Netscape> oppure --cookies-from-browser <browser[+keyring][:profilo]> "
    "(CLI e façade), o con cookiefile / cookiesfrombrowser in [options.<provider>] di config.toml. "
    "Nessun nuovo tentativo finché non lo fai."
)

# Tabella esplicita: (pattern, kind). Il primo che combacia vince.
ERROR_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"too many requests|rate.?limit", re.I), "ratelimit"),
    (re.compile(r"internal server error|bad gateway|service unavailable|gateway time", re.I), "server"),
    (re.compile(r"timed? ?out|connection (reset|refused|aborted)|temporar(y|ily)|remote end closed|"
                r"urlopen error|getaddrinfo|name resolution|\bssl\b|network is unreachable|incomplete read", re.I), "network"),
    (re.compile(r"sign in|not a bot|login required|log in|cookies|members[- ]only|premium|subscriber", re.I), "auth"),
    (re.compile(r"forbidden|not available in your country|geo.?restrict|in your country|"
                r"private (video|track|playlist)|this (track|video|playlist) is private|is private", re.I), "forbidden"),
    (re.compile(r"ffmpeg|ffprobe|postprocess|conversion failed", re.I), "ffmpeg"),
    (re.compile(r"unsupported url|no suitable extractor|is not a valid url", re.I), "unsupported"),
    (re.compile(r"requested format is not available|no (video|audio) formats|no formats found", re.I), "format"),
    (re.compile(r"\bDRM\b", re.I), "drm"),
    (re.compile(r"video unavailable|no longer available|has been removed|does not exist|not found|"
                r"not available|been deleted|is unavailable|no such", re.I), "missing"),
]
# Lo status HTTP, quando c'è, decide prima dei pattern testuali.
STATUS_KINDS = {429: "ratelimit", 401: "auth", 403: "forbidden", 404: "missing", 410: "missing"}
# 403 permanente, TRANNE quando il messaggio dice che servono cookie/verifica anti-bot.
_COOKIES_ON_403 = re.compile(r"sign in to confirm|not a bot|cookies", re.I)

KIND_INFO: dict[str, dict[str, Any]] = {
    "ratelimit": {"retryable": True, "limit": None, "hint": "Rate limit del sito: il core riprova con backoff (Retry-After se presente)."},
    "server": {"retryable": True, "limit": None, "hint": "Errore del server: temporaneo, il core riprova con backoff."},
    "network": {"retryable": True, "limit": None, "hint": "Errore di rete: il core riprova con backoff."},
    "auth": {"retryable": False, "limit": 0, "hint": "Serve un login: esporta i cookie del browser (formato Netscape) e imposta cookiefile in [options.<provider>]."},
    "forbidden": {"retryable": False, "limit": 0, "hint": "Contenuto privato, geo-bloccato o riservato: non viene ritentato. Se hai accesso, usa cookiefile."},
    "need_cookies": {"retryable": False, "limit": 0, "hint": COOKIES_HINT},
    "missing": {"retryable": False, "limit": 0, "hint": "Contenuto rimosso o URL errato: controlla il link."},
    "unsupported": {"retryable": False, "limit": 0, "hint": "yt-dlp non supporta questo sito: https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"},
    "ffmpeg": {"retryable": False, "limit": 0, "hint": "Problema con ffmpeg: verifica l'installazione o ffmpeg_location in config.toml."},
    "format": {"retryable": False, "limit": 0, "hint": "Nessun formato audio disponibile: prova un'altra qualità o aggiorna yt-dlp (pip install -U yt-dlp)."},
    "drm": {"retryable": False, "limit": 0, "hint": "Il contenuto usa DRM: yt-dlp non lo supporta e non verrà ritentato."},
    "unknown": {"retryable": True, "limit": 1, "hint": "Errore non classificato di yt-dlp: un solo nuovo tentativo. Se persiste, aggiorna yt-dlp (pip install -U yt-dlp) e segnala il messaggio."},
}
_HTTP_RE = re.compile(r"HTTP Error (\d{3})")


def classify_error(text: str) -> tuple[str, int | None]:
    """``(kind, status_code)`` dal messaggio grezzo di yt-dlp."""
    text = text or ""
    status = None
    m = _HTTP_RE.search(text)
    if m:
        status = int(m.group(1))
        if 500 <= status < 600:
            return "server", status
        if status == 403 and _COOKIES_ON_403.search(text):
            return "need_cookies", status
        if status in STATUS_KINDS:
            return STATUS_KINDS[status], status
    for pattern, kind in ERROR_RULES:
        if pattern.search(text):
            return kind, status
    return "unknown", status


def _first_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    line = lines[0] if lines else ""
    return re.sub(r"^ERROR:\s*", "", line)


def map_error(exc: BaseException | str, provider: str, *, what: str = "") -> ProviderError:
    """Messaggio di yt-dlp -> ``ProviderError`` classificato (vedi ``ERROR_RULES``).

    Logga sempre il messaggio grezzo con la classificazione: a WARNING quando
    è ``unknown``, così la tabella cresce dai casi reali."""
    raw = exc if isinstance(exc, str) else str(exc)
    kind, status = classify_error(raw)
    info = KIND_INFO[kind]
    message = _first_line(raw) or (type(exc).__name__ if not isinstance(exc, str) else "errore")
    if what:
        message = f"{what}: {message}"
    if kind == "unknown":
        log.warning("yt-dlp errore NON classificato [%s]: %s", provider, raw.strip()[:500])
    else:
        log.info("yt-dlp errore [%s] classificato '%s'%s: %s", provider, kind,
                 f" (HTTP {status})" if status else "", _first_line(raw)[:200])
    if kind == "need_cookies":
        # classe terminale: niente retry, niente backoff, non conta per il breaker
        return NeedsCookies(message, hint=info["hint"], provider=provider, status_code=status)
    return ProviderError(
        message, hint=info["hint"], retryable=bool(info["retryable"]), status_code=status,
        provider=provider, retry_limit=info["limit"], kind=kind,
    )


def safe_extract(ydl_or_opts: Any, target: str, provider: str, *, what: str | None = None) -> dict:
    """``extract_info(download=False)`` robusto.

    Con un dict di opzioni (il caso normale) crea un ``YoutubeDL`` dedicato alla
    singola chiamata, con il proprio ``ExtractLogger``: l'errore recuperato è
    sempre quello di QUESTA estrazione, anche con thread concorrenti. Con
    un'istanza ``YoutubeDL`` già costruita la usa così com'è (solo per usi
    sequenziali/test).

    Converte ogni errore in ``ProviderError`` classificato, trasforma un
    risultato ``None`` (``ignoreerrors``) nell'ultimo errore loggato e
    materializza le ``entries`` quando sono un generatore (``process=False``).
    """
    what = what or f"estrazione di {target}"
    if isinstance(ydl_or_opts, Mapping):
        logger = ExtractLogger()
        ydl = yt_dlp.YoutubeDL({**ydl_or_opts, "logger": logger})
        own = True
    else:
        ydl = ydl_or_opts
        logger = ydl.params.get("logger") if hasattr(ydl, "params") else None
        if isinstance(logger, ExtractLogger):
            logger.last_error = None
        own = False
    try:
        try:
            info = ydl.extract_info(target, download=False)
        except ProviderError:
            raise
        except (DownloadError, ExtractorError) as exc:
            raise map_error(exc, provider, what=what) from None
        except Exception as exc:  # noqa: BLE001 - errori di rete/handler fuori dal protocollo di yt-dlp
            raise map_error(f"{type(exc).__name__}: {exc}", provider, what=what) from None
    finally:
        if own:
            try:
                ydl.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("close() yt-dlp: %s", exc)
    if not info:
        last = logger.last_error if isinstance(logger, ExtractLogger) else None
        if last:
            raise map_error(last, provider, what=what)
        raise ProviderError(f"{what}: nessuna informazione restituita", provider=provider, kind="missing",
                            hint="Controlla che l'URL sia pubblico e supportato da yt-dlp.")
    entries = info.get("entries")
    if entries is not None and not isinstance(entries, list):
        if isinstance(entries, Iterator) or hasattr(entries, "__iter__"):
            info["entries"] = list(entries)
    return info


# ------------------------------------------------------------- info -> Track
def _clean_uploader(name: str | None) -> str | None:
    if not name:
        return None
    text = re.sub(r"\s*-\s*topic$", "", str(name), flags=re.IGNORECASE)
    text = re.sub(r"\s*vevo$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*official$", "", text, flags=re.IGNORECASE)
    return text.strip() or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def slim_info(info: Mapping[str, Any] | None, *, keep_formats: bool = True) -> dict | None:
    """Copia di ``info`` senza le chiavi pesanti, da conservare in ``Track.extra``
    per riusarla al download senza ri-estrarre."""
    if not info:
        return None
    out = {k: v for k, v in info.items() if k not in HEAVY_INFO_KEYS}
    if not keep_formats:
        out.pop("formats", None)
    return out


def reusable(info: Mapping[str, Any] | None) -> bool:
    """``True`` se ``info`` contiene già gli stream (si può saltare l'estrazione)."""
    if not info:
        return False
    if info.get("_type") not in (None, "video"):
        return False
    return bool(info.get("formats")) or bool(info.get("url"))


def info_to_track(
    info: Mapping[str, Any],
    provider: str,
    *,
    provider_id: str | None = None,
    album: str | None = None,
    track_no: int | None = None,
    extra: Mapping[str, Any] | None = None,
    year_from_upload: bool = False,
) -> Track:
    """Converte un ``info_dict`` di yt-dlp in ``Track`` senza inventare campi.

    ``year_from_upload=True`` usa anche ``upload_date``/``timestamp`` come anno:
    sensato dove la data di caricamento è la data di pubblicazione (SoundCloud).
    """
    artists: list[str] = []
    raw_artists = info.get("artists") or info.get("artist") or info.get("creator")
    if isinstance(raw_artists, list):
        artists = [str(a).strip() for a in raw_artists if str(a).strip()]
    elif raw_artists:
        artists = [a.strip() for a in re.split(r"\s*,\s*|\s+&\s+", str(raw_artists)) if a.strip()]
    uploader = _clean_uploader(info.get("uploader") or info.get("channel"))
    if not artists and uploader:
        artists = [uploader]

    duration = info.get("duration")
    duration_ms = int(float(duration) * 1000) if duration is not None else None
    year = _as_int(info.get("release_year"))
    if year is None and info.get("release_date"):
        year = _as_int(str(info["release_date"])[:4])
    if year is None and year_from_upload:
        if info.get("upload_date"):
            year = _as_int(str(info["upload_date"])[:4])
        elif info.get("timestamp"):
            try:
                from datetime import datetime, timezone

                year = datetime.fromtimestamp(float(info["timestamp"]), tz=timezone.utc).year
            except (TypeError, ValueError, OSError, OverflowError):
                year = None

    merged: dict[str, Any] = {
        "url": info.get("webpage_url") or info.get("original_url") or info.get("url"),
        "uploader": info.get("uploader") or info.get("channel"),
        "extractor": info.get("extractor_key") or info.get("ie_key") or info.get("extractor"),
    }
    if info.get("upload_date"):
        merged["upload_year"] = _as_int(str(info["upload_date"])[:4])
    merged.update({k: v for k, v in (extra or {}).items() if v is not None})

    return Track(
        provider=provider,
        provider_id=str(provider_id or info.get("id") or merged["url"] or ""),
        title=str(info.get("track") or info.get("title") or "").strip() or "Untitled",
        artists=artists,
        album=(info.get("album") or album) or None,
        duration_ms=duration_ms,
        isrc=info.get("isrc") or None,
        track_no=_as_int(info.get("track_number")) or track_no,
        disc_no=_as_int(info.get("disc_number")),
        year=year,
        cover_url=info.get("thumbnail") or None,
        extra={k: v for k, v in merged.items() if v is not None},
    )


# ------------------------------------------------------------------- download
def _find_output(tmp: Path, preferred_ext: str | None) -> Path | None:
    files = [p for p in tmp.iterdir() if p.is_file() and p.suffix.lower() not in TEMP_SUFFIXES]
    if not files:
        return None
    exact = [p for p in files if preferred_ext and p.suffix.lower().lstrip(".") == preferred_ext]
    pool = exact or [p for p in files if p.suffix.lower().lstrip(".") in AUDIO_EXTS] or files
    return max(pool, key=lambda p: p.stat().st_mtime)


def _clear_dir(tmp: Path) -> None:
    for p in tmp.iterdir():
        try:
            p.unlink()
        except OSError:
            pass


def download_audio(
    url: str,
    dest: Path,
    quality: str,
    progress_cb: ProgressCallback | None,
    options: Mapping[str, Any] | None,
    provider: str,
    *,
    info: Mapping[str, Any] | None = None,
) -> Path:
    """Scarica l'audio di ``url`` in una cartella temporanea e sposta il file su
    ``dest`` + estensione.

    ``quality``: ``best`` tiene il formato nativo senza transcodifica; un codec
    esplicito preferisce lo stream nativo in quel codec e ricodifica solo se
    la sorgente è diversa.

    Riuso dell'``info`` (best-effort, NON una gestione degli stream scaduti):
    con un ``info_dict`` già risolto si chiama ``process_ie_result`` e non si
    ri-estrae. Se il download fallisce (tipicamente URL degli stream scaduti,
    403/410) si ri-estrae con ``extract_info`` AL MASSIMO UNA VOLTA, l'evento
    viene contato in ``core.stats`` (``stream_reextract``) e, se fallisce
    anche quella, l'errore esce classificato come qualunque altro.
    ``process_ie_result`` è API interna di yt-dlp: il pin in requirements-cli
    e il test d'integrazione proteggono da cambi fuori range.
    """
    q = parse_quality(quality, provider)
    dest = Path(dest)
    tmp = Path(tempfile.mkdtemp(prefix="grabmedia-"))
    produced: dict[str, str] = {}

    def on_progress(d: dict) -> None:
        if progress_cb is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                progress_cb(min(99.0, done / total * 100), "download")
        elif status == "finished":
            progress_cb(99.0, "conversione" if not q.native else "finalizzazione")

    def on_postprocess(d: dict) -> None:
        if d.get("status") == "finished":
            pp_info = d.get("info_dict") or {}
            if pp_info.get("filepath"):
                produced["path"] = pp_info["filepath"]

    pp: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": q.codec}
    if q.bitrate:
        pp["preferredquality"] = str(q.bitrate)
    opts = base_opts(options, noplaylist=True)
    opts.update({
        "format": q.format_selector(),
        "outtmpl": {"default": str(tmp / "%(id)s.%(ext)s")},
        "postprocessors": [pp],
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
        "keepvideo": False,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            reused = False
            if reusable(info):
                try:
                    ydl.process_ie_result(copy.deepcopy(dict(info)), download=True)  # type: ignore[arg-type]
                    reused = True
                except (DownloadError, ExtractorError, KeyError, TypeError, ValueError) as exc:
                    stats.increment("stream_reextract")  # una sola ri-estrazione, contata
                    log.info("[%s] info riusata non valida per %s (%s): ri-estraggo (una volta)",
                             provider, url, _first_line(str(exc))[:160])
                    produced.clear()
                    _clear_dir(tmp)
            if not reused:
                ydl.extract_info(url, download=True)
        out = Path(produced["path"]) if produced.get("path") and Path(produced["path"]).exists() else _find_output(tmp, q.ext)
        if out is None:
            raise ProviderError(
                f"yt-dlp non ha prodotto nessun file per {url}",
                hint="Rilancia con -vv per vedere l'output di yt-dlp.",
                provider=provider,
            )
        ext = out.suffix.lstrip(".") or q.ext or "m4a"
        final = dest.with_name(f"{dest.name}.{ext}")
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            final.unlink()
        shutil.move(str(out), str(final))
        if progress_cb is not None:
            progress_cb(100.0, "completato")
        return final
    except (DownloadError, ExtractorError) as exc:
        raise map_error(exc, provider, what=f"download di {url}") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
