"""Pipeline: URL -> provider -> tracce -> download concorrente con resume.

Il core non sa quale provider sta usando: riceve ``AudioSource`` dal registry,
delega l'audio delle ``MetadataSource`` al provider indicato, applica naming,
stato SQLite, semafori per provider, retry con backoff e circuit breaker.
Con ``dry_run=True`` risolve le tracce e stampa il piano senza scaricare né
scrivere stato.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core import naming, registry, state as state_mod, stats
from core.concurrency import ProviderSemaphores
from core.config import Config
from core.retry import Breakers, call_with_retry
from sources.base import AudioSource, NoMatch, ProviderError, Track

log = logging.getLogger("grabmedia.pipeline")

_TEMP_SUFFIXES = (".part", ".ytdl", ".tmp", ".temp")


@dataclass(slots=True)
class PlannedTrack:
    track: Track
    source: str
    audio_provider: str
    quality: str
    dest: Path
    match_hash: str
    action: str  # "download" | "skip"
    reason: str = ""
    result_path: Path | None = None
    error: str | None = None
    status: str = state_mod.PENDING
    error_kind: str | None = None   # kind dell'errore (es. need_cookies, nomatch, missing)
    hint: str | None = None         # hint azionabile dell'errore, conservato per l'output


@dataclass(slots=True)
class Summary:
    planned: list[PlannedTrack] = field(default_factory=list)
    url_errors: list[tuple[str, str]] = field(default_factory=list)
    url_error_details: list[dict] = field(default_factory=list)  # {url, message, kind, hint, provider}

    @property
    def done(self) -> list[PlannedTrack]:
        return [p for p in self.planned if p.status == state_mod.DONE]

    @property
    def failed(self) -> list[PlannedTrack]:
        return [p for p in self.planned if p.status == state_mod.FAILED]

    @property
    def skipped(self) -> list[PlannedTrack]:
        return [p for p in self.planned if p.status == state_mod.SKIPPED]

    @property
    def ok(self) -> bool:
        return not self.failed and not self.url_errors


class ConsoleProgress:
    """Log ogni ~20% di avanzamento, una riga per traccia, thread-safe."""

    def __init__(self, label: str, step: float = 20.0) -> None:
        self.label = label
        self.step = step
        self._last = -1.0
        self._lock = threading.Lock()

    def __call__(self, percent: float, message: str = "") -> None:
        with self._lock:
            if percent >= 100 or percent - self._last >= self.step:
                self._last = percent
                log.info("%s: %3.0f%% %s", self.label, percent, message)


def _existing_output(dest: Path) -> Path | None:
    """File già presente con lo stesso stem di ``dest`` (qualsiasi estensione)."""
    if not dest.parent.exists():
        return None
    for candidate in dest.parent.glob(f"{dest.name}.*"):
        if candidate.is_file() and candidate.suffix.lower() not in _TEMP_SUFFIXES:
            return candidate
    return None


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        *,
        dry_run: bool = False,
        forced_provider: str | None = None,
        overwrite: bool | None = None,
        state_db: state_mod.StateDB | None = None,
        events: Callable[[str, Any], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.forced = forced_provider
        self.overwrite = cfg.overwrite if overwrite is None else overwrite
        # Hook opzionale: ("planned", Summary), ("progress", (PlannedTrack, percent)),
        # ("done", PlannedTrack). Usato dalla façade per lo stato dei task.
        self.events = events
        self.semaphores = ProviderSemaphores(cfg.concurrency_for)
        self.breakers = Breakers(threshold=cfg.circuit_threshold, cooldown=cfg.circuit_cooldown)
        self._state = state_db
        self._owns_state = state_db is None

    # ---------------------------------------------------------------- state
    @property
    def state(self) -> state_mod.StateDB | None:
        if self.dry_run:
            return None
        if self._state is None:
            self._state = state_mod.StateDB(self.cfg.state_db)
            self._state.reset_running()
        return self._state

    def close(self) -> None:
        if self._owns_state and self._state is not None:
            self._state.close()
            self._state = None
        for source in list(registry.REGISTRY.values()):
            try:
                source.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("close() di '%s' ha fallito: %s", source.name, exc)

    # -------------------------------------------------------------- resolve
    def _retry(self, provider: str, fn, what: str):
        return call_with_retry(
            fn,
            provider=provider,
            breaker=self.breakers.get(provider),
            max_attempts=self.cfg.max_attempts,
            base=self.cfg.backoff_base,
            cap=self.cfg.backoff_cap,
            what=what,
        )

    def resolve(self, url: str) -> tuple[AudioSource, AudioSource, list[Track]]:
        source = registry.dispatch(url, self.forced, self.cfg.priority)
        audio = registry.audio_provider_for(source, self.cfg.priority)
        tracks = self._retry(source.name, lambda: source.resolve(url), what=f"resolve({url})")
        if not tracks:
            raise ProviderError(
                f"'{source.name}' non ha trovato tracce per {url}",
                hint="Controlla che l'URL sia pubblico e punti a una traccia, album o playlist.",
                provider=source.name,
            )
        log.info("[%s] %d tracce da %s%s", source.name, len(tracks), url,
                 "" if audio is source else f" (audio via '{audio.name}')")
        return source, audio, tracks

    def plan_track(self, track: Track, source: AudioSource, audio: AudioSource) -> PlannedTrack:
        quality = self.cfg.quality_for(audio.name)
        if self.dry_run:
            dest = Path(self.cfg.out_dir).joinpath(*naming.render(track, self.cfg.naming))
        else:
            dest = naming.build(track, self.cfg.naming, self.cfg.out_dir)
        mh = state_mod.match_hash(track, audio.name, quality, dest)
        planned = PlannedTrack(track, source.name, audio.name, quality, dest, mh, "download")

        if not self.overwrite:
            db = self.state
            done_path = db.is_done(track, mh) if db is not None else None
            existing = done_path or _existing_output(dest)
            if existing is not None:
                planned.action = "skip"
                planned.reason = "già scaricata" if done_path else "file già presente"
                planned.result_path = existing
                planned.status = state_mod.SKIPPED
        return planned

    def plan(self, urls: list[str]) -> Summary:
        summary = Summary()
        for url in urls:
            try:
                source, audio, tracks = self.resolve(url)
            except ProviderError as exc:
                log.error("%s", exc)
                summary.url_errors.append((url, str(exc)))
                summary.url_error_details.append({"url": url, "message": exc.message, "kind": exc.kind,
                                                  "hint": exc.hint, "provider": exc.provider})
                continue
            except Exception as exc:  # noqa: BLE001
                log.error("Errore inatteso su %s: %s", url, exc, exc_info=log.isEnabledFor(logging.DEBUG))
                summary.url_errors.append((url, f"{type(exc).__name__}: {exc}"))
                summary.url_error_details.append({"url": url, "message": f"{type(exc).__name__}: {exc}",
                                                  "kind": "unknown", "hint": None, "provider": None})
                continue
            for track in tracks:
                try:
                    summary.planned.append(self.plan_track(track, source, audio))
                except ValueError as exc:  # template di naming errato
                    raise ProviderError(str(exc), hint="Correggi 'general.naming' in config.toml") from None
        self._emit("planned", summary)
        return summary

    def _emit(self, event: str, payload: Any) -> None:
        if self.events is None:
            return
        try:
            self.events(event, payload)
        except Exception as exc:  # noqa: BLE001 - un osservatore rotto non ferma la pipeline
            log.debug("hook eventi (%s) ha fallito: %s", event, exc)

    # ------------------------------------------------------------- download
    def _download_one(self, item: PlannedTrack) -> PlannedTrack:
        audio = registry.REGISTRY[item.audio_provider]
        source = registry.REGISTRY.get(item.source, audio)
        # Una MetadataSource delega da sé (può intercettare NoMatch, loggare,
        # applicare soglie); il semaforo e il breaker restano del provider audio.
        downloader = source if not source.supports_direct_audio else audio
        db = self.state
        label = item.track.display()
        console = ConsoleProgress(label)

        def progress(percent: float, message: str = "") -> None:
            console(percent, message)
            self._emit("progress", (item, percent))

        with self.semaphores.slot(audio.name):
            if db is not None:
                db.upsert(item.track, status=state_mod.RUNNING, match_hash=item.match_hash)
            try:
                result = self._retry(
                    audio.name,
                    lambda: downloader.download(item.track, item.dest, item.quality, progress),
                    what=f"download '{label}'",
                )
                result = Path(result)
                if not result.exists():
                    raise ProviderError(
                        f"'{audio.name}' ha dichiarato il file {result} ma non esiste",
                        provider=audio.name,
                    )
            except NoMatch as exc:
                item.status = state_mod.SKIPPED
                item.reason = "nessun match affidabile"
                item.error = str(exc)
                item.error_kind = "nomatch"
                item.hint = exc.hint
                if db is not None:
                    db.mark(item.track.key, state_mod.SKIPPED, error=item.error, bump_attempts=True)
                log.warning("SALTATA %s: %s", label, exc)
                self._emit("done", item)
                return item
            except Exception as exc:  # noqa: BLE001
                item.status = state_mod.FAILED
                item.error = str(exc)
                item.error_kind = getattr(exc, "kind", None) or "failed"
                item.hint = getattr(exc, "hint", None)
                if db is not None:
                    db.mark(item.track.key, state_mod.FAILED, error=item.error, bump_attempts=True)
                log.error("FALLITA %s: %s", label, exc)
                self._emit("done", item)
                return item
            item.status = state_mod.DONE
            item.result_path = result
            if db is not None:
                db.mark(item.track.key, state_mod.DONE, path=result, match_hash=item.match_hash, bump_attempts=True)
            log.info("OK %s -> %s", label, result)
            self._emit("done", item)
            return item

    def run(self, urls: list[str]) -> Summary:
        summary = self.plan(urls)
        to_download = [p for p in summary.planned if p.action == "download"]

        if self.dry_run:
            for item in summary.planned:
                item.status = state_mod.SKIPPED if item.action == "skip" else state_mod.PENDING
            return summary
        if not summary.planned:
            return summary

        db = self.state
        if db is not None:
            for item in to_download:
                db.upsert(item.track, status=state_mod.PENDING, match_hash=item.match_hash)
            for item in summary.planned:
                if item.action == "skip":
                    db.upsert(item.track, status=state_mod.DONE, match_hash=item.match_hash)
                    if item.result_path:
                        db.mark(item.track.key, state_mod.DONE, path=item.result_path)

        if not to_download:
            return summary

        providers = {p.audio_provider for p in to_download}
        workers = max(1, sum(self.cfg.concurrency_for(p) for p in providers))
        futures: list[Future[PlannedTrack]] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="grabmedia") as pool:
            for item in to_download:
                futures.append(pool.submit(self._download_one, item))
            for fut in futures:
                fut.result()
        return summary


def format_plan(summary: Summary, *, dry_run: bool) -> str:
    lines = []
    header = "PIANO (dry-run, nessun download)" if dry_run else "RISULTATO"
    lines.append(header)
    for item in summary.planned:
        if item.status == state_mod.DONE:
            mark, note = "OK  ", str(item.result_path)
        elif item.status == state_mod.FAILED:
            mark, note = "ERR ", item.error or ""
        elif item.action == "skip":
            mark, note = "SKIP", f"{item.reason}: {item.result_path}"
        elif item.status == state_mod.SKIPPED:
            mark, note = "SKIP", f"{item.reason}: {item.error or ''}"
        else:
            mark, note = "DL  ", f"{item.dest} [{item.audio_provider}, {item.quality}]"
        lines.append(f"  {mark} {item.track.display()}  ->  {note}")
    for url, err in summary.url_errors:
        lines.append(f"  ERR {url}: {err}")
    lines.append(
        f"Totale: {len(summary.planned)} tracce, "
        f"{len(summary.done)} ok, {len(summary.skipped)} saltate, "
        f"{len(summary.failed)} fallite, {len(summary.url_errors)} URL in errore"
    )
    counters = {k: v for k, v in stats.snapshot().items() if v}
    if counters:
        lines.append("Statistiche: " + ", ".join(f"{k}={v}" for k, v in sorted(counters.items())))
    return "\n".join(lines)
