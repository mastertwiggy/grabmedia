"""Provider Bandcamp.

Legge il JSON ``data-tralbum`` della pagina album/brano con un parser HTML
vero (le entity dell'attributo sono già decodificate dal parser) per
titolo, artista, album, numero traccia, anno, ``art_id`` e ``trackinfo``.

ATTENZIONE alla qualità: ``trackinfo[].file["mp3-128"]`` NON è il file
originale, è lo stesso stream mp3 a 128 kbps del player. L'originale
(FLAC/WAV/320) esiste solo nel flusso ``/download`` con token, che questo
provider non implementa. Quindi:

* ``is_downloadable`` vero + ``file`` presente significa solo "c'è un mp3-128
  raggiungibile senza passare da yt-dlp";
* il download diretto viene usato solo quando la qualità richiesta è
  compatibile con un mp3-128 nativo (``best`` oppure ``mp3`` senza bitrate o
  con bitrate <= 128): si consegna esattamente quel file, senza ricodifica;
* per qualsiasi altro codec si passa a yt-dlp, che per Bandcamp scarica lo
  stesso stream mp3-128 e ricodifica nel codec chiesto (lossy su lossy:
  è una scelta esplicita dell'utente, non del provider).

Se Bandcamp risponde con la pagina di verifica ("Client Challenge") il
provider lo ricorda per la sessione e usa yt-dlp per tutto il resto, senza
ritentare il fetch diretto a ogni brano. Nessuna ricerca: ``accepts_delegation``
è ``False`` (Bandcamp è autoritativo sulle proprie pagine).
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import logging
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

from sources._ytdlp import (
    base_opts,
    download_audio,
    effective_cookiefile,
    find_ffmpeg,
    info_to_track,
    parse_quality,
    require_ytdlp,
    safe_extract,
    slim_info,
)
from sources.base import AudioSource, ProgressCallback, ProviderError, Track

log = logging.getLogger("grabmedia.bandcamp")

COVER_URL = "https://f4.bcbits.com/img/a{art_id}_10.jpg"
USER_AGENT = "Mozilla/5.0 (compatible; GrabMedia/0.2; +https://github.com/yt-dlp/yt-dlp)"
EXCLUDED_HOSTS = {"bandcamp.com", "www.bandcamp.com", "daily.bandcamp.com", "blog.bandcamp.com", "get.bandcamp.com"}
DIRECT_MAX_BITRATE = 128  # lo stream "file" è sempre mp3-128
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_PATH_RE = re.compile(r"^/(album|track)/([A-Za-z0-9._~-]+)/?$")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CHALLENGE_MARKERS = ("client challenge", "challenge-platform", "cf-chl", "just a moment")


def classify(url: str) -> str | None:
    """``"album"``, ``"track"`` o ``None``."""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return None
    host = parts.netloc.lower()
    if parts.scheme not in ("http", "https") or not host.endswith(".bandcamp.com") or host in EXCLUDED_HOSTS:
        return None
    m = _PATH_RE.match(parts.path or "")
    return m.group(1) if m else None


def _page_title(page: str) -> str | None:
    m = _TITLE_RE.search(page or "")
    return html.unescape(m.group(1)).strip()[:80] if m else None


def is_challenge_page(page: str) -> bool:
    """Pagina di verifica anti-bot al posto dell'album."""
    if not page or "data-tralbum" in page:
        return False
    lowered = page.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


# ------------------------------------------------------------------ HTTP
def _opener(options: Mapping[str, Any] | None):
    """Opener urllib del percorso diretto: stessi cookie di yt-dlp (``cookiefile``,
    oppure il file Netscape temporaneo ricavato da ``cookiesfrombrowser``)."""
    handlers: list[Any] = []
    cookiefile = effective_cookiefile(options)
    if cookiefile:
        jar = http.cookiejar.MozillaCookieJar(str(cookiefile))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise ProviderError(
                f"cookiefile non leggibile: {cookiefile} ({exc})",
                hint="Esporta i cookie in formato Netscape (estensione 'Get cookies.txt') e correggi [options.bandcamp].",
                provider="bandcamp",
            ) from None
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def fetch_text(url: str, options: Mapping[str, Any] | None = None, *, timeout: float = 30.0) -> str:
    """GET di una pagina HTML (sostituibile nei test)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with _opener(options).open(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def fetch_to_file(url: str, dest: Path, options: Mapping[str, Any] | None = None, *,
                  progress_cb: ProgressCallback | None = None, timeout: float = 60.0) -> Path:
    """Scarica ``url`` su ``dest`` a blocchi, riportando l'avanzamento."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _opener(options).open(req, timeout=timeout) as resp, dest.open("wb") as fh:
        total = resp.headers.get("Content-Length")
        total_n = int(total) if total and total.isdigit() else None
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress_cb is not None and total_n:
                progress_cb(min(99.0, done / total_n * 100), "download")
    return dest


def _http_error(exc: BaseException, what: str) -> ProviderError:
    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        retryable = status == 429 or 500 <= status < 600
        hint = None
        if status in (401, 403):
            hint = "Contenuto riservato (acquisto/abbonamento): esporta i cookie del browser in cookiefile di [options.bandcamp]."
        elif status == 404:
            hint = "Pagina non trovata: controlla l'URL."
        elif retryable:
            hint = "Errore temporaneo: il core riprova con backoff."
        return ProviderError(f"{what}: HTTP {status}", hint=hint, retryable=retryable, status_code=status,
                             provider="bandcamp", kind="ratelimit" if status == 429 else "server" if retryable else "forbidden" if status in (401, 403) else "missing")
    if isinstance(exc, urllib.error.URLError):
        return ProviderError(f"{what}: errore di rete ({exc.reason})", retryable=True, provider="bandcamp", kind="network",
                             hint="Controlla la connessione; il core riprova con backoff.")
    return ProviderError(f"{what}: {exc}", provider="bandcamp")


# --------------------------------------------------------------- parsing
class _DataAttrParser(HTMLParser):
    """Raccoglie ``data-tralbum`` e ``data-embed`` (il parser decodifica le
    entity degli attributi: ``&quot;`` -> ``"``)."""

    WANTED = ("data-tralbum", "data-embed")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: dict[str, dict] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name not in self.WANTED or not value:
                continue
            key = name[len("data-"):]
            if key in self.found:
                continue
            data = _loads_lenient(value)
            if isinstance(data, dict):
                self.found[key] = data


def _loads_lenient(value: str) -> Any:
    """JSON dell'attributo. Il parser ha già decodificato le entity; il
    fallback ``html.unescape`` serve solo a un attributo codificato due volte
    (``&amp;quot;``) e viene tentato solo se il primo parse fallisce."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(html.unescape(value))
    except json.JSONDecodeError:
        return None


def extract_data_attrs(page: str) -> dict[str, dict]:
    """``{"tralbum": {...}, "embed": {...}}`` dagli attributi ``data-*`` della pagina."""
    parser = _DataAttrParser()
    try:
        parser.feed(page or "")
        parser.close()
    except Exception as exc:  # noqa: BLE001 - HTML malformato: meglio "niente" che un crash
        log.debug("parser HTML: %s", exc)
    return parser.found


def _year(*values: Any) -> int | None:
    for value in values:
        if not value:
            continue
        m = _YEAR_RE.search(str(value))
        if m:
            return int(m.group(0))
    return None


def tracks_from_page(page: str, page_url: str, provider: str = "bandcamp") -> list[Track]:
    """Costruisce le ``Track`` dal JSON ``data-tralbum`` (e ``data-embed`` per il titolo album)."""
    attrs = extract_data_attrs(page)
    tralbum = attrs.get("tralbum")
    if not tralbum:
        raise ProviderError(
            f"nessun data-tralbum nella pagina {page_url}",
            hint="Non sembra una pagina album/brano di Bandcamp, oppure il contenuto richiede il login (cookiefile).",
            provider=provider,
        )
    embed = attrs.get("embed") or {}
    current = tralbum.get("current") or {}
    item_type = (tralbum.get("item_type") or current.get("type") or "").lower()
    page_artist = tralbum.get("artist") or embed.get("artist")
    if item_type == "album":
        album_title = current.get("title") or embed.get("album_title")
    else:
        album_title = embed.get("album_title") or tralbum.get("album_title")
    year = _year(tralbum.get("album_release_date"), current.get("release_date"), current.get("publish_date"))
    art_id = tralbum.get("art_id") or current.get("art_id")
    cover = COVER_URL.format(art_id=art_id) if art_id else None
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}/"

    tracks: list[Track] = []
    for info in tralbum.get("trackinfo") or []:
        if not info or not info.get("id") or not info.get("title"):
            continue
        link = info.get("title_link")
        page_link = urljoin(base, link) if link else (page_url if item_type == "track" else None)
        files = info.get("file") or {}
        stream = files.get("mp3-128") or next((v for v in files.values() if v), None) if isinstance(files, dict) else None
        duration = info.get("duration")
        artist = info.get("artist") or page_artist
        extra = {
            "url": page_link,
            "stream_url": stream,            # mp3-128 del player, NON l'originale
            "stream_bitrate": DIRECT_MAX_BITRATE if stream else None,
            "downloadable": bool(info.get("is_downloadable")),
            "art_id": art_id,
            "source_url": page_url,
            "albumartist": page_artist,
        }
        tracks.append(
            Track(
                provider=provider,
                provider_id=str(info["id"]),
                title=str(info["title"]).strip(),
                artists=[str(artist).strip()] if artist else [],
                album=album_title or None,
                duration_ms=int(float(duration) * 1000) if duration else None,
                track_no=int(info["track_num"]) if info.get("track_num") else None,
                year=year,
                cover_url=cover,
                extra={k: v for k, v in extra.items() if v is not None},
            )
        )
    return tracks


# -------------------------------------------------------------- provider
class BandcampSource(AudioSource):
    name = "bandcamp"
    supports_direct_audio = True
    accepts_delegation = False

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        require_ytdlp(self.name)
        self.ffmpeg = find_ffmpeg(self.options, self.name)
        self.challenged = False  # pagina di verifica vista in questa sessione

    def can_handle(self, url: str) -> bool:
        return classify(url) is not None

    def resolve(self, url: str) -> list[Track]:
        if classify(url) is None:
            raise ProviderError(f"URL Bandcamp non riconosciuto: {url}", provider=self.name,
                                hint="Accetto https://<artista>.bandcamp.com/album/<slug> o /track/<slug>.")
        if self.challenged:
            log.debug("[bandcamp] sessione con challenge attiva: %s via yt-dlp", url)
            return self._resolve_via_ytdlp(url)
        try:
            page = fetch_text(url, self.options)
        except (urllib.error.URLError, OSError) as exc:
            raise _http_error(exc, f"pagina {url}") from None
        if not extract_data_attrs(page).get("tralbum"):
            if is_challenge_page(page):
                self.challenged = True
                log.warning("[bandcamp] pagina di verifica (%s) per %s: per il resto della sessione uso yt-dlp",
                            _page_title(page) or "Client Challenge", url)
            else:
                log.info("[bandcamp] nessun data-tralbum in %s (%s): provo con yt-dlp",
                         url, _page_title(page) or "pagina senza titolo")
            return self._resolve_via_ytdlp(url)
        tracks = tracks_from_page(page, url, self.name)
        if not tracks:
            raise ProviderError(f"nessun brano in {url}", provider=self.name,
                                hint="La pagina non espone tracce (preorder o contenuto riservato?).")
        return tracks

    def _resolve_via_ytdlp(self, url: str) -> list[Track]:
        opts = base_opts(self.options, extract_flat=False, noplaylist=False, ignoreerrors=True)
        try:
            info = safe_extract(opts, url, self.name)  # YoutubeDL e logger dedicati alla chiamata
        except ProviderError as err:
            if err.kind in (None, "unknown", "missing"):
                err.hint = ("Bandcamp non ha restituito i dati dell'album (pagina di verifica?): riprova più tardi "
                            "oppure esporta i cookie del browser in cookiefile di [options.bandcamp].")
            raise
        is_collection = info.get("entries") is not None
        entries = [e for e in (info.get("entries") or []) if e] if is_collection else [info]
        album = info.get("title") if is_collection else None
        tracks: list[Track] = []
        for index, entry in enumerate(entries, 1):
            if not entry.get("id"):
                continue
            tracks.append(info_to_track(
                entry, self.name, provider_id=str(entry["id"]),
                album=entry.get("album") or album, track_no=index if album else None,
                extra={"url": entry.get("webpage_url") or entry.get("url"), "source_url": url,
                       "downloadable": False, "ytdlp_info": slim_info(entry)},
            ))
        if not tracks:
            raise ProviderError(f"nessun brano in {url}", provider=self.name,
                                hint="La pagina non espone tracce (preorder o contenuto riservato?).")
        return tracks

    def close(self) -> None:
        """Nessuna sessione persistente: ogni estrazione ha il proprio YoutubeDL."""

    # -------------------------------------------------------------- download
    @staticmethod
    def direct_allowed(quality: str) -> bool:
        """Il diretto consegna un mp3-128 nativo: ha senso solo per ``best`` o
        ``mp3`` senza bitrate / con bitrate <= 128."""
        q = parse_quality(quality, "bandcamp")
        if q.native:
            return True
        return q.codec == "mp3" and (q.bitrate is None or q.bitrate <= DIRECT_MAX_BITRATE)

    def download(
        self,
        track: Track,
        dest: Path,
        quality: str,
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        if track.provider != self.name:
            raise ProviderError(
                f"bandcamp non ha una ricerca: non può cercare '{track.display()}' di '{track.provider}'",
                hint="Imposta audio_provider su un provider con ricerca (es. youtube o soundcloud).",
                provider=self.name,
            )
        page_url = track.extra.get("url") or track.extra.get("source_url")
        if not page_url:
            raise ProviderError(f"traccia senza URL di pagina: {track.display()}", provider=self.name)
        stream = track.extra.get("stream_url")
        if stream and track.extra.get("downloadable") and self.direct_allowed(quality):
            try:
                return self._download_direct(str(stream), Path(dest), progress_cb)
            except ProviderError as exc:
                if exc.retryable:
                    raise
                log.warning("[bandcamp] download diretto fallito (%s), passo a yt-dlp", exc.message)
        return download_audio(str(page_url), Path(dest), quality, progress_cb, self.options, self.name,
                              info=track.extra.get("ytdlp_info"))

    def _download_direct(self, stream_url: str, dest: Path, progress_cb: ProgressCallback | None) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="grabmedia-bc-"))
        try:
            try:
                got = fetch_to_file(stream_url, tmp / "stream.mp3", self.options, progress_cb=progress_cb)
            except (urllib.error.URLError, OSError) as exc:
                raise _http_error(exc, "download diretto") from None
            if got.stat().st_size == 0:
                raise ProviderError("download diretto vuoto", provider=self.name)
            final = dest.with_name(f"{dest.name}.mp3")
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                final.unlink()
            shutil.move(str(got), str(final))
            if progress_cb is not None:
                progress_cb(100.0, "completato")
            return final
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
