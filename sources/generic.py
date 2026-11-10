"""Provider passthrough: qualsiasi URL http/https che yt-dlp sa estrarre.

Nessun matching: scarica solo URL diretti, quindi ``accepts_delegation`` è
``False`` e il registry non lo sceglie mai come ``audio_provider``.
``can_handle`` risponde ``True`` solo se nessun altro provider caricato
rivendica l'URL.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from core import registry
from sources._ytdlp import (
    base_opts,
    download_audio,
    find_ffmpeg,
    info_to_track,
    require_ytdlp,
    safe_extract,
    slim_info,
)
from sources.base import AudioSource, ProgressCallback, ProviderError, Track

log = logging.getLogger("grabmedia.generic")


class GenericSource(AudioSource):
    name = "generic"
    supports_direct_audio = True
    accepts_delegation = False
    fallback = True

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        require_ytdlp(self.name)
        self.ffmpeg = find_ffmpeg(self.options, self.name)

    def _extract(self, url: str) -> dict:
        # un YoutubeDL con logger dedicato per ogni chiamata (vedi safe_extract)
        return safe_extract(base_opts(self.options, extract_flat=True, noplaylist=False), url, self.name)

    def close(self) -> None:
        """Nessuna sessione persistente: ogni estrazione ha il proprio YoutubeDL."""

    # ------------------------------------------------------------- contratto
    def can_handle(self, url: str) -> bool:
        try:
            parts = urlparse(url.strip())
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        for other in registry.REGISTRY.values():
            if other is self:
                continue
            try:
                if other.can_handle(url):
                    return False
            except Exception:  # noqa: BLE001 - un provider rotto non blocca il passthrough
                continue
        return True

    def resolve(self, url: str) -> list[Track]:
        info = self._extract(url)
        entries = [e for e in (info.get("entries") or []) if e] if info.get("entries") is not None else None
        if entries is None:
            page = info.get("webpage_url") or info.get("original_url") or url
            return [info_to_track(info, self.name, provider_id=page, extra={"url": page, "ytdlp_info": slim_info(info)})]
        tracks: list[Track] = []
        for index, entry in enumerate(entries, 1):
            page = entry.get("webpage_url") or entry.get("url") or entry.get("original_url")
            if not page:
                continue
            tracks.append(
                info_to_track(
                    entry, self.name, provider_id=page,
                    extra={"url": page, "playlist": info.get("title"), "playlist_index": index},
                )
            )
        return tracks

    def download(
        self,
        track: Track,
        dest: Path,
        quality: str,
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        if track.provider != self.name:
            raise ProviderError(
                f"generic scarica solo URL diretti: non può cercare '{track.display()}' di '{track.provider}'",
                hint="Imposta audio_provider su un provider con ricerca (es. uno che accetta deleghe).",
                provider=self.name,
            )
        url = str(track.extra.get("url") or track.provider_id)
        return download_audio(url, Path(dest), quality, progress_cb, self.options, self.name,
                              info=track.extra.get("ytdlp_info"))
