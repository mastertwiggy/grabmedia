"""Provider SoundCloud via yt-dlp.

Brani singoli, set (playlist/album) e profili utente. Accetta deleghe: per una
``Track`` di altro provider cerca con ``scsearchN:`` (estrazione piatta: una
sola chiamata) e sceglie con ``sources.matching.pick``.

Scelte che contano:

* i set in ``extract_flat`` sono solo URL senza titolo, quindi si estraggono
  completi; l'``info`` di ogni brano viene conservato in ``Track.extra`` e
  riusato al download (niente seconda estrazione, niente finestra in cui un
  brano sparisce tra resolve e download). Sopra ``max_full_tracks`` gli
  stream non vengono conservati (meno memoria) e il download ri-estrae;
* un brano rotto non affonda il set: si prosegue con gli altri, si logga
  l'elenco dei mancanti e il set fallisce solo se manca la maggioranza;
* ``on.soundcloud.com``: si segue il redirect e si classifica l'URL finale;
* i 429 (frequenti) sono retryable e li gestiscono backoff e breaker del core;
  403 (privato, geo-block) e 404 sono permanenti e non aprono il breaker.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote_plus, urlparse

from sources import matching
from sources._ytdlp import (
    base_opts,
    download_audio,
    find_ffmpeg,
    info_to_track,
    require_ytdlp,
    safe_extract,
    slim_info,
)
from sources.base import AudioSource, NoMatch, ProgressCallback, ProviderError, Track

log = logging.getLogger("grabmedia.soundcloud")

HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}
API_TRACK_URL = "https://api.soundcloud.com/tracks/{id}"
SEARCH_URL = "https://soundcloud.com/search?q={q}"
USER_AGENT = "Mozilla/5.0 (compatible; GrabMedia/0.2)"
DEFAULT_SEARCH_RESULTS = 10
DEFAULT_MAX_FULL_TRACKS = 150
# Primo segmento di path che NON è un utente.
RESERVED = {
    "discover", "search", "you", "stream", "upload", "charts", "pages", "terms-of-use",
    "settings", "messages", "notifications", "people", "tags", "jobs", "imprint", "popular",
    "mobile", "login", "signin", "logout", "feed", "stations",
}
# Secondo segmento che indica una collezione dell'utente (lista di tracce).
USER_COLLECTIONS = {"tracks", "likes", "albums", "sets", "reposts", "popular-tracks", "followers", "following"}


def classify(url: str) -> str | None:
    """``"track"``, ``"set"``, ``"user"`` (profilo o sua collezione), ``"short"``
    (on.soundcloud.com, tipo noto solo dopo il redirect) o ``None``."""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return None
    host = parts.netloc.lower()
    if parts.scheme not in ("http", "https") or host not in HOSTS:
        return None
    segments = [s for s in (parts.path or "").split("/") if s]
    if host == "on.soundcloud.com":
        return "short" if segments else None
    if not segments or segments[0] in RESERVED:
        return None
    if len(segments) == 1:
        return "user"
    if segments[1] == "sets":
        return "set" if len(segments) >= 3 else "user"
    if segments[1] in USER_COLLECTIONS:
        return "user"
    if len(segments) == 2:
        return "track"
    return None


def expand_short_url(url: str, options: Mapping[str, Any] | None = None, *, timeout: float = 15.0) -> str | None:
    """URL finale di un link breve ``on.soundcloud.com`` (segue i redirect);
    ``None`` se la rete non risponde (si ricade sull'estrazione yt-dlp)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = resp.geturl()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("redirect di %s non seguito: %s", url, exc)
        return None
    parts = urlparse(final)
    return parts._replace(query="", fragment="").geturl() if parts.netloc else None


class SoundcloudSource(AudioSource):
    name = "soundcloud"
    supports_direct_audio = True
    accepts_delegation = True

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        require_ytdlp(self.name)
        self.ffmpeg = find_ffmpeg(self.options, self.name)
        self.threshold = float(self.options.get("match_threshold", matching.DEFAULT_THRESHOLD))
        self.search_results = max(1, int(self.options.get("search_results", DEFAULT_SEARCH_RESULTS)))
        self.max_full_tracks = max(1, int(self.options.get("max_full_tracks", DEFAULT_MAX_FULL_TRACKS)))

    # ------------------------------------------------------------ sessioni
    def _opts(self, mode: str) -> dict:
        """Opzioni yt-dlp per modalità; ``safe_extract`` crea un ``YoutubeDL``
        con logger dedicato a ogni chiamata (niente istanze condivise tra thread).
        ``"track"``: brano singolo (noplaylist, errori sollevati);
        ``"flat"``: elenco piatto / ricerca; ``"full"``: collezione completa.
        Le collezioni usano ``ignoreerrors`` così un brano rotto diventa un
        ``None`` nelle entries invece di far fallire tutto."""
        if mode == "flat":
            return base_opts(self.options, extract_flat=True, noplaylist=False, ignoreerrors=True)
        if mode == "full":
            return base_opts(self.options, extract_flat=False, noplaylist=False, ignoreerrors=True)
        return base_opts(self.options, extract_flat=False, noplaylist=True)

    def _extract(self, target: str, *, flat: bool = False, mode: str | None = None) -> dict:
        mode = mode or ("flat" if flat else "track")
        return safe_extract(self._opts(mode), target, self.name)

    def close(self) -> None:
        """Nessuna sessione persistente: ogni estrazione ha il proprio YoutubeDL."""

    # ------------------------------------------------------------- contratto
    def can_handle(self, url: str) -> bool:
        return classify(url) is not None

    def resolve(self, url: str) -> list[Track]:
        kind = classify(url)
        if kind is None:
            raise ProviderError(f"URL SoundCloud non riconosciuto: {url}", provider=self.name,
                                hint="Accetto brani, set e profili di soundcloud.com oppure link on.soundcloud.com.")
        if kind == "short":
            final = expand_short_url(url, self.options)
            if final and classify(final) not in (None, "short"):
                log.info("[soundcloud] %s -> %s", url, final)
                url, kind = final, classify(final)
            else:
                kind = "unknown"
        if kind == "track":
            return [self._single(self._extract(url, mode="track"))]
        if kind == "set":
            return self._resolve_set(url)
        # profilo o link breve non classificabile: estrazione piatta; un brano
        # singolo torna comunque completo, i profili come elenco con titoli.
        info = self._extract(url, mode="flat")
        if info.get("entries") is None:
            return [self._single(info)]
        if self._looks_like_set(info) or self._mostly_untitled(info):
            return self._resolve_set(url)
        return self._collection(info, numbered=False, reuse=False)

    # --------------------------------------------------------------- resolve
    @staticmethod
    def _looks_like_set(info: Mapping[str, Any]) -> bool:
        page = str(info.get("webpage_url") or info.get("original_url") or "")
        return "/sets/" in page

    @staticmethod
    def _mostly_untitled(info: Mapping[str, Any]) -> bool:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return False
        untitled = sum(1 for e in entries if not e.get("title"))
        return untitled * 2 > len(entries)

    def _single(self, info: Mapping[str, Any]) -> Track:
        return info_to_track(
            info, self.name, provider_id=str(info.get("id") or ""), year_from_upload=True,
            extra={"ytdlp_info": slim_info(info)},
        )

    def _resolve_set(self, url: str) -> list[Track]:
        """Set con contenimento per brano: elenco piatto (ids e posizioni, una
        chiamata) + estrazione completa; i brani mancanti vengono elencati e il
        set fallisce solo se ne manca la maggioranza."""
        flat = self._extract(url, mode="flat")
        if flat.get("entries") is None:
            return [self._single(flat)]
        expected = [str(e.get("id")) for e in (flat.get("entries") or []) if e and e.get("id")]
        positions = {tid: i for i, tid in enumerate(expected, 1)}

        full = self._extract(url, mode="full")
        if full.get("entries") is None:
            return [self._single(full)]
        entries = [e for e in (full.get("entries") or []) if e and e.get("id")]
        got = {str(e.get("id")) for e in entries}
        missing = [tid for tid in expected if tid not in got]
        if expected and len(missing) * 2 > len(expected):
            raise ProviderError(
                f"{len(missing)} brani su {len(expected)} del set non estraibili: {', '.join(missing[:10])}",
                hint="Il set è per lo più privato, geo-bloccato o rimosso; prova con cookiefile o controlla l'URL.",
                provider=self.name, kind="missing",
            )
        if missing:
            log.warning("[soundcloud] set %s: %d brani su %d non estraibili, continuo con gli altri: %s",
                        full.get("title") or url, len(missing), len(expected),
                        ", ".join(API_TRACK_URL.format(id=t) for t in missing[:20]))
        reuse = len(entries) <= self.max_full_tracks
        if not reuse:
            log.info("[soundcloud] set con %d brani > max_full_tracks=%d: gli stream non vengono conservati",
                     len(entries), self.max_full_tracks)
        tracks = self._collection(full, numbered=True, reuse=reuse, positions=positions)
        return tracks

    def _collection(
        self,
        info: Mapping[str, Any],
        *,
        numbered: bool,
        reuse: bool,
        positions: Mapping[str, int] | None = None,
    ) -> list[Track]:
        entries = [e for e in (info.get("entries") or []) if e]
        label = info.get("title")
        tracks: list[Track] = []
        index = 0
        for entry in entries:
            track_id = entry.get("id")
            if not track_id:
                continue
            index += 1
            position = (positions or {}).get(str(track_id), index)
            extra: dict[str, Any] = {"playlist": label, "playlist_index": position}
            if not (entry.get("webpage_url") or entry.get("url")):
                extra["url"] = API_TRACK_URL.format(id=track_id)
            if reuse:
                extra["ytdlp_info"] = slim_info(entry, keep_formats=True)
            tracks.append(
                info_to_track(
                    entry, self.name, provider_id=str(track_id),
                    track_no=position if numbered else None, extra=extra, year_from_upload=True,
                )
            )
        return tracks

    # ---------------------------------------------------------------- match
    def _search(self, track: Track) -> str:
        query = " ".join(p for p in (track.artist, track.title) if p).strip()
        threshold = self.threshold
        try:
            threshold = max(threshold, float(track.extra.get("match_threshold", threshold)))
        except (TypeError, ValueError):
            pass
        # Ricerca piatta: una sola chiamata API, i campi (titolo, uploader,
        # durata) bastano al matcher; gli stream li risolve il download.
        info = self._extract(f"scsearch{self.search_results}:{query}", mode="flat")
        candidates = [e for e in (info.get("entries") or []) if e]
        best, score = matching.pick(
            query, candidates, track.duration_ms,
            artist=track.artist or None, title=track.title, threshold=threshold,
        )
        if best is None:
            raise NoMatch(
                f"nessun risultato affidabile su SoundCloud per '{query}' "
                f"(miglior punteggio {score:.2f}, soglia {threshold:.2f})",
                hint=f"Cerca a mano: {SEARCH_URL.format(q=quote_plus(query))} e passa l'URL del brano, "
                "oppure abbassa match_threshold in [options.soundcloud].",
                provider=self.name, query=query, score=score,
            )
        track.extra["matched_track_id"] = str(best["id"])
        track.extra["match_score"] = round(score, 3)
        track.extra.setdefault("matched_title", best.get("title"))
        log.info("[soundcloud] match %.2f per '%s' -> %s (%s)", score, query, best["id"], best.get("title"))
        return str(best.get("webpage_url") or best.get("url") or API_TRACK_URL.format(id=best["id"]))

    def download(
        self,
        track: Track,
        dest: Path,
        quality: str,
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        if track.provider != self.name:
            url = self._search(track)
            info = None
        else:
            url = str(track.extra.get("url") or API_TRACK_URL.format(id=track.provider_id))
            info = track.extra.get("ytdlp_info")
        return download_audio(url, Path(dest), quality, progress_cb, self.options, self.name, info=info)
