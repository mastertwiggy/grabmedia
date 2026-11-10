"""Façade: l'unica superficie pubblica per l'app.

* ``resolve(url)`` -> ``list[Track]``
* ``download(track, fmt, dest)`` -> ``Path``
* ``TaskQueue``: coda in-process con un modello di task (``Task``) e stati
  ``queued | running | done | failed | partial``.

Nessuna ``ProviderError`` esce da qui. L'app importa solo questo modulo, mai
``sources/``.

CONTRATTO ERRORI (regola unica)
-------------------------------
* Un errore che ABORTISCE LA RICHIESTA (URL non gestito o rifiutato, provider
  non disponibile, risoluzione fallita, nessuna traccia) diventa ``ApiError``
  nelle funzioni ``resolve``/``download`` e ``state = failed`` +
  ``message`` nel task (``tracks_total = 0``, ``failures = []``).
* Il FALLIMENTO DI UNA SINGOLA TRACCIA (download, nessun match) finisce in
  ``Task.failures[]`` (``kind`` ``failed`` o ``nomatch``) e non interrompe le
  altre tracce; lo stato finale è ``partial`` se almeno una è riuscita,
  ``failed`` se nessuna.
* Caso ambiguo (tutte le tracce di una richiesta falliscono): VINCE
  ``failures[]``. La richiesta non è abortita, è stata eseguita con esito
  negativo: ``state = failed``, ``failures`` popolate, ``message`` riassuntivo,
  nessuna eccezione. Solo ``download()`` di una singola traccia, che non ha
  una lista in cui registrare il fallimento, solleva ``ApiError``.
* Precedenza dell'hint su un abort: l'hint del PROVIDER (es. ``need_cookies``
  con il comando esatto) viene prima; quello del dispatch, se c'è, segue
  concatenato. Non viene mai appiattito nel messaggio.

HOST NON RISOLVIBILE (scelta)
-----------------------------
Il guard anti-SSRF della façade LASCIA PASSARE un host che non si risolve
(``check_public_url`` senza ``require_resolvable``). Perché: un host che non
si risolve non può raggiungere nessun indirizzo, quindi non è un vettore
SSRF; la CLI gira sulla macchina dell'utente, dove il DNS può essere
diverso da quello del provider (VPN, hosts, split DNS) e un rifiuto qui
sarebbe un falso negativo con un messaggio peggiore di quello che darà il
provider. La web app invece serve utenti non fidati e pretende la
risoluzione (``require_resolvable=True`` in ``app/models.py``). Il test
``test_unresolvable_host_policy`` fissa entrambe le scelte.
"""

from __future__ import annotations

import ipaddress
import logging
import queue
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from core import config as config_mod, naming, registry
from core.pipeline import Pipeline, PlannedTrack, Summary
from core import state as state_mod
from sources.base import ProviderError, Track

log = logging.getLogger("grabmedia.api")

__all__ = ["ApiError", "Task", "TaskFailure", "TaskQueue", "Track", "resolve", "download"]

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
PARTIAL = "partial"


class ApiError(Exception):
    """Errore della façade: mai una ``ProviderError`` grezza."""

    def __init__(self, message: str, *, hint: str | None = None, kind: str | None = None, provider: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.kind = kind
        self.provider = provider

    @classmethod
    def from_provider(cls, exc: ProviderError) -> "ApiError":
        return cls(exc.message, hint=exc.hint, kind=exc.kind or ("unavailable" if exc.__class__.__name__ == "ProviderUnavailable" else None),
                   provider=exc.provider)

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "hint": self.hint, "kind": self.kind, "provider": self.provider}


# ------------------------------------------------------------- anti-SSRF
RESOLVER = socket.getaddrinfo
"""Resolver usato da ``check_public_url`` (sostituibile nei test)."""


def is_public_address(ip_text: str) -> bool:
    """``False`` per loopback (127/8, ::1), privati (10/8, 172.16/12, 192.168/16,
    fc00::/7), link-local (169.254/16, fe80::/10), riservati, multicast, non
    specificati. Nessuna allowlist: non c'è una ragione per ammetterli."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def check_public_url(url: str, resolver: Any = None, *, require_resolvable: bool = False) -> None:
    """UNICA implementazione anti-SSRF del repo (anche l'app passa da qui).

    Rifiuta con ``ApiError`` (kind ``blocked``) gli URL il cui host è, o si
    risolve in, un indirizzo non pubblico. Host non risolvibili passano (non
    possono raggiungere niente e il provider darà il suo errore), salvo
    ``require_resolvable=True`` (kind ``unresolvable``), che è il comportamento
    della web app."""
    resolver = resolver or RESOLVER
    try:
        parts = urlparse(url.strip())
    except ValueError:
        raise ApiError(f"URL non valido: {url}", kind="usage") from None
    if parts.scheme not in ("http", "https"):
        raise ApiError(f"schema non ammesso: {parts.scheme or '(nessuno)'}", kind="usage",
                       hint="Sono ammessi solo URL http e https.")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ApiError(f"host mancante in {url}", kind="usage")
    if host == "localhost" or host.endswith(".localhost"):
        raise ApiError(f"host non consentito: {host}", kind="blocked", hint="Indirizzi locali e privati sono rifiutati.")
    addresses: list[str] = []
    try:
        ipaddress.ip_address(host.strip("[]"))
        addresses = [host.strip("[]")]
    except ValueError:
        try:
            infos = resolver(host, None, 0, socket.SOCK_STREAM)
        except (socket.gaierror, OSError, ValueError) as exc:
            if require_resolvable:
                raise ApiError(f"host non risolvibile: {host}", kind="unresolvable",
                               hint="Controlla l'URL o la risoluzione DNS.") from None
            return  # non risolvibile: nessun rischio SSRF, ci penserà il provider
        addresses = [str(info[4][0]) for info in infos if info and info[4]]
        if require_resolvable and not addresses:
            raise ApiError(f"host non risolvibile: {host}", kind="unresolvable")
    blocked = [a for a in addresses if not is_public_address(a)]
    if blocked:
        raise ApiError(
            f"host non consentito: {host} -> {', '.join(blocked)}", kind="blocked",
            hint="L'host punta a un indirizzo privato, loopback o link-local: rifiutato per sicurezza (SSRF).",
        )


def _config(cfg: config_mod.Config | None) -> config_mod.Config:
    if cfg is not None:
        return cfg
    cfg = config_mod.load()
    config_mod.load_env()
    return cfg


def prepare_cookie_store() -> int:
    """All'avvio di CLI e code: sweep dei file cookie orfani (più vecchi di un
    giorno) nella directory dedicata e pulizia anche su SIGTERM/SIGINT.
    Ritorna quanti file orfani sono stati rimossi."""
    from sources import _ytdlp

    removed = _ytdlp.sweep_cookie_dir()
    _ytdlp.install_signal_cleanup()
    return removed


def renew_browser_cookies() -> int:
    """Rinnovo esplicito dei cookie letti dal browser: scarta i file di questa
    sessione E quelli rimasti nella directory dedicata; la prossima necessità
    rilegge il browser. Ritorna quanti file sono stati rimossi."""
    from sources import _ytdlp

    return _ytdlp.renew_browser_cookies() + _ytdlp.sweep_cookie_dir(max_age_seconds=0)


def js_runtime_available() -> bool:
    """``True`` se un runtime JavaScript per yt-dlp è rilevabile (env, deno, node)."""
    from core import jsruntime

    return jsruntime.detect() is not None


def js_runtime_missing_message() -> str:
    """Messaggio completo per "runtime JavaScript assente": perché e come rimediare
    (le tre istruzioni di installazione, la variabile YTDLP_JS_RUNTIME)."""
    from core import jsruntime

    return str(jsruntime.JsRuntimeMissing())


def _configure_registry(
    cfg: config_mod.Config,
    *,
    cookies: Path | str | None = None,
    cookies_from_browser: str | None = None,
    disabled: Mapping[str, str] | None = None,
) -> None:
    """Opzioni di config.toml nel registry, più i cookie come default per tutti
    i provider (``OPTIONS["*"]``, come ``--cookies`` / ``--cookies-from-browser``)
    e i provider disabilitati dall'applicazione (``nome -> motivo``)."""
    for name, reason in (disabled or {}).items():
        registry.disable(name, reason)
    defaults = dict(registry.OPTIONS.get("*", {}))
    if cookies:
        path = Path(cookies)
        if not path.is_file():
            raise ApiError(f"cookies: file non trovato: {path}", kind="usage",
                           hint="Esporta i cookie dal browser in formato Netscape e indica il percorso.")
        defaults["cookiefile"] = str(path.resolve())
    if cookies_from_browser:
        defaults["cookiesfrombrowser"] = str(cookies_from_browser)
    if not registry.OPTIONS and (cfg.options or defaults):
        options = dict(cfg.options)
        if defaults:
            options["*"] = defaults
        registry.configure(options, cfg.priority)
    else:
        if defaults:
            registry.OPTIONS["*"] = defaults
        if cfg.priority and not registry.PRIORITY:
            registry.PRIORITY[:] = list(cfg.priority)


# ------------------------------------------------------------------ funzioni
def resolve(
    url: str,
    *,
    cfg: config_mod.Config | None = None,
    provider: str | None = None,
    cookies: Path | str | None = None,
    cookies_from_browser: str | None = None,
) -> list[Track]:
    """Tracce di ``url`` (traccia, album, playlist, profilo). ``ApiError`` se
    nessun provider la gestisce o la risoluzione fallisce. ``cookies`` (file
    Netscape) e ``cookies_from_browser`` passano a yt-dlp per tutti i provider."""
    cfg = _config(cfg)
    _configure_registry(cfg, cookies=cookies, cookies_from_browser=cookies_from_browser)
    check_public_url(url)
    try:
        source = registry.dispatch(url, provider, cfg.priority)
        tracks = source.resolve(url)
    except ProviderError as exc:
        raise ApiError.from_provider(exc) from None
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"{type(exc).__name__}: {exc}", kind="unknown") from None
    if not tracks:
        raise ApiError(f"nessuna traccia per {url}", kind="missing", provider=source.name,
                       hint="Controlla che l'URL sia pubblico e punti a una traccia, album o playlist.")
    return tracks


def download(
    track: Track,
    fmt: str | None,
    dest: Path | str | None,
    *,
    cfg: config_mod.Config | None = None,
    progress_cb: Any = None,
    cookies: Path | str | None = None,
    cookies_from_browser: str | None = None,
) -> Path:
    """Scarica ``track`` con la qualità ``fmt`` (``None`` = da config) in
    ``dest`` (senza estensione; ``None`` = percorso dal template di naming).
    Applica retry e circuit breaker del core. ``ApiError`` in caso di errore."""
    cfg = _config(cfg)
    _configure_registry(cfg, cookies=cookies, cookies_from_browser=cookies_from_browser)
    try:
        source = registry.load(track.provider)
        if source is None:
            raise ProviderError(
                f"provider '{track.provider}' non disponibile: {registry.UNAVAILABLE.get(track.provider, 'sconosciuto')}",
                provider=track.provider, kind="unavailable",
            )
        audio = registry.audio_provider_for(source, cfg.priority)
        downloader = source if not source.supports_direct_audio else audio
        quality = fmt or cfg.quality_for(audio.name)
        target = Path(dest) if dest else naming.build(track, cfg.naming, cfg.out_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        pipe = Pipeline(cfg)
        try:
            return Path(pipe._retry(  # noqa: SLF001 - stesso retry/breaker della pipeline
                audio.name,
                lambda: downloader.download(track, target, quality, progress_cb),
                what=f"download '{track.display()}'",
            ))
        finally:
            pipe.close()
    except ProviderError as exc:
        raise ApiError.from_provider(exc) from None
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"{type(exc).__name__}: {exc}", kind="unknown") from None


# --------------------------------------------------------------------- task
@dataclass(slots=True)
class TaskFailure:
    track: str
    message: str
    kind: str | None = None      # es. need_cookies, nomatch, missing, failed
    key: str | None = None
    hint: str | None = None      # hint azionabile (per need_cookies: il comando esatto da usare)


@dataclass(slots=True)
class Task:
    id: str
    url: str
    provider: str | None = None
    state: str = QUEUED
    progress: float = 0.0
    tracks_total: int = 0
    tracks_done: int = 0
    failures: list[TaskFailure] = field(default_factory=list)
    message: str | None = None
    kind: str | None = None      # kind dell'errore che ha abortito la richiesta
    hint: str | None = None      # hint dell'errore che ha abortito la richiesta
    fmt: str | None = None
    files: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["progress"] = round(float(self.progress), 1)
        return data

    @property
    def finished(self) -> bool:
        return self.state in (DONE, FAILED, PARTIAL)


class TaskQueue:
    """Coda in-process: ``submit(url)`` -> ``Task``; un worker (thread) per volta
    esegue la pipeline su ogni URL e aggiorna il task. ``get``/``list``
    restituiscono copie coerenti (lock)."""

    def __init__(
        self,
        cfg: config_mod.Config | None = None,
        *,
        provider: str | None = None,
        out_dir: Path | str | None = None,
        autostart: bool = True,
        cookies: Path | str | None = None,
        cookies_from_browser: str | None = None,
        disabled_providers: Mapping[str, str] | None = None,
    ) -> None:
        self.cfg = _config(cfg)
        if out_dir:
            self.cfg.out_dir = Path(out_dir)
        _configure_registry(self.cfg, cookies=cookies, cookies_from_browser=cookies_from_browser,
                            disabled=disabled_providers)
        prepare_cookie_store()
        self.provider = provider
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        if autostart:
            self.start()

    # ----------------------------------------------------------- ciclo
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="grabmedia-tasks", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _worker(self) -> None:
        while not self._stop.is_set():
            task_id = self._queue.get()
            if task_id is None:
                break
            task = self._tasks.get(task_id)
            if task is None:
                continue
            try:
                self._run(task)
            except Exception as exc:  # noqa: BLE001 - ultima rete: il task non resta "running"
                log.exception("task %s: errore inatteso", task.id)
                self._update(task, state=FAILED, message=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ API
    def submit(self, url: str, fmt: str | None = None) -> Task:
        task = Task(id=uuid.uuid4().hex, url=url.strip(), fmt=fmt)
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
        self._queue.put(task.id)
        return self._copy(task)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return self._copy(task) if task else None

    def list(self, limit: int = 50) -> list[Task]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._copy(self._tasks[i]) for i in ids]

    def wait(self, task_id: str, timeout: float = 60.0, poll: float = 0.05) -> Task | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.get(task_id)
            if task is None or task.finished:
                return task
            time.sleep(poll)
        return self.get(task_id)

    # ------------------------------------------------------------ interni
    def _copy(self, task: Task) -> Task:
        return Task(**{**asdict(task), "failures": [TaskFailure(**f) for f in asdict(task)["failures"]]})

    def _update(self, task: Task, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(task, key, value)
            task.updated_at = time.time()

    def _run(self, task: Task) -> None:
        self._update(task, state=RUNNING)
        try:
            check_public_url(task.url)
        except ApiError as exc:
            self._update(task, state=FAILED, message=exc.message, progress=100.0)
            return
        current: dict[str, float] = {}

        def events(event: str, payload: Any) -> None:
            if event == "planned":
                summary: Summary = payload
                planned = summary.planned
                provider = planned[0].source if planned else None
                self._update(task, tracks_total=len(planned), provider=provider or task.provider)
            elif event == "progress":
                item, percent = payload
                current[item.track.key] = float(percent)
                self._refresh_progress(task, current)
            elif event == "done":
                item: PlannedTrack = payload
                current.pop(item.track.key, None)
                with self._lock:
                    if item.status == state_mod.DONE:
                        task.tracks_done += 1
                        if item.result_path:
                            task.files.append(str(item.result_path))
                    else:
                        # NeedsCookies & co. non vengono appiattiti: kind e hint restano nel failure
                        task.failures.append(TaskFailure(
                            track=item.track.display(), message=item.error or item.reason,
                            kind=item.error_kind or ("nomatch" if item.status == state_mod.SKIPPED else "failed"),
                            key=item.track.key, hint=item.hint,
                        ))
                self._refresh_progress(task, current)

        pipeline = Pipeline(self.cfg, forced_provider=self.provider, events=events)
        try:
            summary = pipeline.run([task.url])
        except ProviderError as exc:
            self._update(task, state=FAILED, message=exc.message, kind=exc.kind, hint=exc.hint, progress=100.0)
            return
        finally:
            pipeline.close()

        with self._lock:
            # tracce già presenti (skip di resume) contano come fatte
            for item in summary.planned:
                if item.action == "skip":
                    task.tracks_done += 1
                    if item.result_path:
                        task.files.append(str(item.result_path))
            # Con più download in parallelo i file arrivano in ordine di completamento:
            # l'elenco finale segue l'ordine dei brani (album e playlist restano in sequenza).
            position = {str(item.result_path): index for index, item in enumerate(summary.planned)
                        if item.result_path}
            task.files.sort(key=lambda path: position.get(path, len(position)))
            if summary.url_errors:
                task.message = summary.url_errors[0][1]
                if summary.url_error_details:
                    detail = summary.url_error_details[0]
                    # messaggio e hint separati: l'hint ha il suo campo e non va ripetuto nel messaggio
                    task.message = detail.get("message") or task.message
                    task.kind = detail.get("kind")
                    task.hint = detail.get("hint")
            if summary.url_errors and not summary.planned:
                task.state = FAILED
            elif task.failures and task.tracks_done:
                task.state = PARTIAL
            elif task.failures or (summary.url_errors and not task.tracks_done):
                task.state = FAILED
            else:
                task.state = DONE
            if task.state != DONE and not task.message and task.failures:
                task.message = f"{len(task.failures)} tracce su {task.tracks_total} non scaricate"
            task.progress = 100.0
            task.updated_at = time.time()

    def _refresh_progress(self, task: Task, current: dict[str, float]) -> None:
        with self._lock:
            total = task.tracks_total or 1
            finished = task.tracks_done + len(task.failures)
            partial = sum(min(99.0, p) for p in current.values()) / 100.0
            task.progress = min(99.9, (finished + partial) / total * 100.0)
            task.updated_at = time.time()
