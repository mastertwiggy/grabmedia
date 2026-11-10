"""Provider YouTube (youtube.com, youtu.be, music.youtube.com) via yt-dlp.

Accetta anche tracce di altri provider (delega): costruisce la query
"artista titolo", cerca con ``ytsearchN:`` e sceglie il video con
``sources.matching.pick``. Se nessun risultato supera la soglia solleva
``NoMatch`` (non retryable) e la pipeline marca la traccia ``skipped``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote_plus, urlparse

from sources import matching
from sources._ytdlp import (
    DownloadError,
    base_opts,
    download_audio,
    find_ffmpeg,
    info_to_track,
    map_error,
    require_ytdlp,
    safe_extract,
    slim_info,
)
from sources.base import AudioSource, NoMatch, ProgressCallback, Track

log = logging.getLogger("grabmedia.youtube")

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
VIDEO_PATHS = ("/shorts/", "/embed/", "/live/", "/v/")
CHANNEL_PREFIXES = ("/channel/", "/c/", "/user/", "/@")
ALBUM_PLAYLIST_PREFIX = "OLAK5uy_"
WATCH_URL = "https://www.youtube.com/watch?v={id}"
SEARCH_URL = "https://www.youtube.com/results?search_query={q}"


def classify(url: str) -> str | None:
    """``"video"``, ``"playlist"``, ``"channel"`` o ``None`` se non è YouTube."""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.netloc.lower() not in HOSTS:
        return None
    host = parts.netloc.lower()
    path = parts.path or "/"
    query = parse_qs(parts.query)
    if host.endswith("youtu.be"):
        return "video" if len(path.strip("/")) >= 8 else None
    if path == "/watch" and query.get("v"):
        return "video"
    if any(path.startswith(p) for p in VIDEO_PATHS) and len(path.strip("/").split("/")) >= 2:
        return "video"
    if path == "/playlist" and query.get("list"):
        return "playlist"
    if host == "music.youtube.com" and path.startswith("/browse/"):
        return "playlist"  # album / playlist di YouTube Music
    if any(path.startswith(p) for p in CHANNEL_PREFIXES):
        return "channel"
    return None


class YoutubeSource(AudioSource):
    name = "youtube"
    supports_direct_audio = True
    accepts_delegation = True

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        require_ytdlp(self.name)
        self.ffmpeg = find_ffmpeg(self.options, self.name)
        self.threshold = float(self.options.get("match_threshold", matching.DEFAULT_THRESHOLD))
        self.search_results = max(1, int(self.options.get("search_results", 5)))
    # ------------------------------------------------------------ sessioni
    def _opts(self, *, flat: bool) -> dict:
        """``flat``: playlist/canali in extract_flat; altrimenti video singoli e
        ricerche con dati completi. ``safe_extract`` crea un YoutubeDL con
        logger dedicato per ogni chiamata."""
        if flat:
            return base_opts(self.options, extract_flat=True, noplaylist=False)
        return base_opts(self.options, extract_flat=False, noplaylist=True)

    def _extract(self, target: str, *, flat: bool) -> dict:
        return safe_extract(self._opts(flat=flat), target, self.name)

    def close(self) -> None:
        """Nessuna sessione persistente: ogni estrazione ha il proprio YoutubeDL."""

    # ------------------------------------------------------------- contratto
    def can_handle(self, url: str) -> bool:
        return classify(url) is not None

    def resolve(self, url: str) -> list[Track]:
        kind = classify(url)
        if kind is None:
            raise map_error(DownloadError(f"URL non YouTube: {url}"), self.name)
        if kind == "video":
            info = self._extract(url, flat=False)
            # l'info completo (con gli stream) viene riusato al download: niente seconda estrazione
            return [info_to_track(info, self.name, provider_id=info.get("id"), extra={"ytdlp_info": slim_info(info)})]

        info = self._extract(url, flat=True)
        return self._tracks_from_collection(info, url, depth=0)

    def _tracks_from_collection(self, info: dict, url: str, *, depth: int) -> list[Track]:
        entries = [e for e in (info.get("entries") or []) if e]
        collection_id = str(info.get("id") or "")
        is_album = collection_id.startswith(ALBUM_PLAYLIST_PREFIX) or (
            "music.youtube.com" in url and info.get("_type") == "playlist" and bool(info.get("album"))
        )
        tracks: list[Track] = []
        index = 0
        for entry in entries:
            if entry.get("entries") is not None or (entry.get("_type") == "url" and entry.get("ie_key") == "YoutubeTab"):
                if depth >= 1:
                    log.debug("Ignoro la sotto-collezione annidata %s", entry.get("id") or entry.get("url"))
                    continue
                sub = self._extract(entry.get("url") or entry.get("webpage_url"), flat=True) if entry.get("entries") is None else entry
                tracks.extend(self._tracks_from_collection(sub, url, depth=depth + 1))
                continue
            video_id = entry.get("id")
            if not video_id:
                continue
            index += 1
            extra = {"playlist": info.get("title"), "playlist_id": collection_id or None, "playlist_index": index}
            if not entry.get("webpage_url") and not entry.get("url"):
                extra["url"] = WATCH_URL.format(id=video_id)
            tracks.append(
                info_to_track(
                    entry,
                    self.name,
                    provider_id=video_id,
                    album=info.get("title") if is_album else None,
                    track_no=index if is_album else None,
                    extra=extra,
                )
            )
        return tracks

    # ---------------------------------------------------------------- match
    def _search(self, track: Track) -> str:
        query = " ".join(p for p in (track.artist, track.title) if p).strip()
        # La sorgente dei metadati può chiedere una soglia più alta (track.extra).
        threshold = self.threshold
        try:
            threshold = max(threshold, float(track.extra.get("match_threshold", threshold)))
        except (TypeError, ValueError):
            pass
        info = self._extract(f"ytsearch{self.search_results}:{query}", flat=False)
        candidates = [e for e in (info.get("entries") or []) if e]
        best, score = matching.pick(
            query, candidates, track.duration_ms,
            artist=track.artist or None, title=track.title, threshold=threshold,
        )
        if best is None:
            raise NoMatch(
                f"nessun risultato affidabile per '{query}' "
                f"(miglior punteggio {score:.2f}, soglia {threshold:.2f})",
                hint=f"Cerca a mano: {SEARCH_URL.format(q=quote_plus(query))} e passa l'URL del video, "
                "oppure abbassa match_threshold in [options.youtube].",
                provider=self.name, query=query, score=score,
            )
        track.extra["matched_video_id"] = best["id"]
        track.extra["match_score"] = round(score, 3)
        track.extra.setdefault("matched_title", best.get("title"))
        # il risultato della ricerca è già completo: riusabile al download
        track.extra["ytdlp_info"] = slim_info(best)
        log.info("[youtube] match %.2f per '%s' -> %s (%s)", score, query, best["id"], best.get("title"))
        return str(best["id"])

    def download(
        self,
        track: Track,
        dest: Path,
        quality: str,
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        video_id = self._search(track) if track.provider != self.name else track.provider_id
        url = WATCH_URL.format(id=video_id)
        return download_audio(url, Path(dest), quality, progress_cb, self.options, self.name,
                              info=track.extra.get("ytdlp_info"))
