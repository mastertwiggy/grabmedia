"""Provider Spotify senza credenziali: metadati dalla pagina embed pubblica.

Scarica ``https://open.spotify.com/embed/{tipo}/{id}`` (la pagina del player
incorporabile, pubblica), estrae il JSON di ``__NEXT_DATA__`` e mappa
``entity.trackList`` in ``Track``. Solo metadati: l'audio è delegato a un
provider con ricerca, come per ``sources/spotify.py``.

Cosa c'è e cosa no nella pagina embed (verificato il 2026-11-10):

* ``trackList[]``: ``uri``, ``title``, ``subtitle`` (artisti separati da
  ``",\\xa0"``), ``duration`` in millisecondi. Gli item senza ``uri`` vengono saltati.
* album: nome, artista dell'album (``subtitle``), copertina; nessuna data.
* traccia singola: artisti, data di pubblicazione, copertina; nessun nome album.
* playlist: nome, proprietario, immagine DELLA PLAYLIST. Non è la copertina dei
  brani, quindi finisce in ``extra["playlist_cover_url"]`` e ``cover_url`` resta
  ``None``: nessun campo inventato.
* niente ISRC, niente numero di disco, niente album per i brani di una playlist.

La pagina contiene anche un token di sessione anonimo del player: questo
modulo NON lo legge e non lo usa. Legge solo ``entity``.

Fallback sulla Web API (spotipy, ``sources/spotify.py``): solo se
``mode = "spotify_api"`` è impostato esplicitamente in ``[options.spotify_embed]``
E le credenziali sono presenti in .env. La Web API richiede Premium
sull'account proprietario dell'app.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlparse

from sources.base import MetadataSource, ProviderError, ProviderUnavailable, Track

log = logging.getLogger("grabmedia.spotify_embed")

EMBED_URL = "https://open.spotify.com/embed/{kind}/{sid}"
HOSTS = {"open.spotify.com", "play.spotify.com"}
KINDS = ("track", "album", "playlist")
USER_AGENT = "Mozilla/5.0 (compatible; GrabMedia/0.2)"
MODE_EMBED = "embed"
MODE_API = "spotify_api"
ARTIST_SEPARATOR = ", "  # virgola + spazio non separabile: i nomi con ", " normale restano interi

_PATH_RE = re.compile(r"^/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?(track|album|playlist)/([A-Za-z0-9]{22})/?$")
_URI_RE = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")
_NEXT_DATA_RE = re.compile(r'<script\b[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I)
_HINT_LAYOUT = (
    "Spotify ha probabilmente cambiato la pagina embed, oppure ha servito una pagina diversa (verifica, "
    "errore). Riprova tra poco e aggiorna GrabMedia. In alternativa usa la Web API ufficiale: metti "
    "SPOTIFY_CLIENT_ID e SPOTIFY_CLIENT_SECRET in .env e imposta mode = \"spotify_api\" in "
    "[options.spotify_embed] (richiede Spotify Premium sull'account proprietario dell'app)."
)


def parse_url(url: str) -> tuple[str, str] | None:
    """``(tipo, id)`` per track, album e playlist di open.spotify.com (o URI ``spotify:``)."""
    text = (url or "").strip()
    m = _URI_RE.match(text)
    if m:
        return m.group(1), m.group(2)
    try:
        parts = urlparse(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.netloc.lower() not in HOSTS:
        return None
    m = _PATH_RE.match(parts.path or "")
    return (m.group(1), m.group(2)) if m else None


def canonical_url(kind: str, sid: str) -> str:
    return f"https://open.spotify.com/{kind}/{sid}"


def fetch_embed(url: str, *, timeout: float = 30.0) -> str:
    """GET della pagina embed (sostituibile nei test)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _http_error(exc: BaseException, what: str) -> ProviderError:
    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        if status == 429 or 500 <= status < 600:
            return ProviderError(f"{what}: HTTP {status}", retryable=True, status_code=status, provider="spotify_embed",
                                 kind="ratelimit" if status == 429 else "server",
                                 hint="Errore temporaneo di Spotify: il core riprova con backoff.")
        if status == 404:
            return ProviderError(f"{what}: non trovato (HTTP 404)", status_code=404, provider="spotify_embed", kind="missing",
                                 hint="Controlla il link. Le playlist private non hanno una pagina embed pubblica.")
        return ProviderError(f"{what}: HTTP {status}", status_code=status, provider="spotify_embed", kind="forbidden",
                             hint="Spotify ha rifiutato la richiesta della pagina embed: riprova più tardi.")
    if isinstance(exc, urllib.error.URLError):
        return ProviderError(f"{what}: errore di rete ({exc.reason})", retryable=True, provider="spotify_embed", kind="network",
                             hint="Controlla la connessione; il core riprova con backoff.")
    return ProviderError(f"{what}: {exc}", provider="spotify_embed")


def extract_entity(page: str, url: str) -> dict[str, Any]:
    """``entity`` dal JSON ``__NEXT_DATA__`` della pagina embed.

    Solleva ``ProviderError`` (kind ``layout_changed``) con un messaggio
    azionabile se lo script manca, non è JSON o non contiene ``entity``."""
    m = _NEXT_DATA_RE.search(page or "")
    if not m:
        raise ProviderError(
            f"la pagina embed di Spotify non contiene __NEXT_DATA__ ({url})",
            hint=_HINT_LAYOUT, provider="spotify_embed", kind="layout_changed",
        )
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"__NEXT_DATA__ della pagina embed non è JSON valido ({exc.msg}) ({url})",
            hint=_HINT_LAYOUT, provider="spotify_embed", kind="layout_changed",
        ) from None
    entity = None
    try:
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError):
        pass
    if not isinstance(entity, dict) or not entity.get("type"):
        raise ProviderError(
            f"__NEXT_DATA__ non contiene props.pageProps.state.data.entity ({url})",
            hint=_HINT_LAYOUT, provider="spotify_embed", kind="layout_changed",
        )
    return entity


def split_artists(subtitle: str | None) -> list[str]:
    """``"A,\\xa0B"`` -> ``["A", "B"]``; ``"Tyler, The Creator"`` resta un solo nome."""
    if not subtitle:
        return []
    return [part.strip() for part in str(subtitle).split(ARTIST_SEPARATOR) if part.strip()]


def _largest_image(entity: Mapping[str, Any]) -> str | None:
    images = [i for i in ((entity.get("visualIdentity") or {}).get("image") or []) if i and i.get("url")]
    if images:
        return max(images, key=lambda i: (i.get("maxWidth") or 0) * (i.get("maxHeight") or 0))["url"]
    sources = [s for s in ((entity.get("coverArt") or {}).get("sources") or []) if s and s.get("url")]
    return sources[0]["url"] if sources else None


def _year(release: Any) -> int | None:
    iso = release.get("isoString") if isinstance(release, Mapping) else release
    m = re.match(r"^(\d{4})", str(iso)) if iso else None
    return int(m.group(1)) if m else None


def _track_id(uri: str | None) -> str | None:
    m = _URI_RE.match(str(uri or "").strip())
    return m.group(2) if m and m.group(1) == "track" else None


def tracks_from_entity(entity: Mapping[str, Any], source_url: str, provider: str = "spotify_embed") -> list[Track]:
    """Mappa l'``entity`` della pagina embed in ``Track`` (vedi il docstring del modulo)."""
    kind = str(entity.get("type") or "").lower()
    image = _largest_image(entity)

    if kind == "track":
        track_id = _track_id(entity.get("uri")) or entity.get("id")
        if not track_id:
            return []
        artists = [a["name"].strip() for a in (entity.get("artists") or []) if a and a.get("name")]
        extra = {"url": canonical_url("track", track_id), "spotify_uri": entity.get("uri"), "source_url": source_url,
                 "explicit": entity.get("isExplicit")}
        return [Track(
            provider=provider, provider_id=str(track_id),
            title=str(entity.get("title") or entity.get("name") or "").strip() or "Untitled",
            artists=artists or split_artists(entity.get("subtitle")),
            duration_ms=int(entity["duration"]) if entity.get("duration") else None,
            year=_year(entity.get("releaseDate")),
            cover_url=image,
            extra={k: v for k, v in extra.items() if v is not None},
        )]

    is_album = kind == "album"
    collection_name = entity.get("name") or entity.get("title")
    owner = entity.get("subtitle")
    tracks: list[Track] = []
    for position, item in enumerate(entity.get("trackList") or [], 1):
        if not item:
            continue
        track_id = _track_id(item.get("uri"))
        if not track_id:  # item senza uri (o non traccia): non identificabile, si salta
            continue
        extra: dict[str, Any] = {"url": canonical_url("track", track_id), "spotify_uri": item.get("uri"),
                                 "source_url": source_url, "explicit": item.get("isExplicit")}
        if is_album:
            extra["albumartist"] = owner
        else:
            extra.update({"playlist": collection_name, "playlist_owner": owner, "playlist_index": position,
                          "playlist_cover_url": image})
        tracks.append(Track(
            provider=provider, provider_id=track_id,
            title=str(item.get("title") or "").strip() or "Untitled",
            artists=split_artists(item.get("subtitle")),
            album=(collection_name or None) if is_album else None,
            duration_ms=int(item["duration"]) if item.get("duration") else None,
            track_no=position if is_album else None,
            year=_year(entity.get("releaseDate")) if is_album else None,
            cover_url=image if is_album else None,
            extra={k: v for k, v in extra.items() if v is not None},
        ))
    return tracks


class SpotifyEmbedSource(MetadataSource):
    name = "spotify_embed"
    supports_direct_audio = False
    accepts_delegation = True

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        self.audio_provider = self.options.get("audio_provider") or None
        self.mode = str(self.options.get("mode") or MODE_EMBED).strip().lower()
        if self.mode not in (MODE_EMBED, MODE_API):
            raise ProviderUnavailable(
                f"mode = \"{self.mode}\" non riconosciuto in [options.spotify_embed]",
                hint=f"Valori validi: \"{MODE_EMBED}\" (default) oppure \"{MODE_API}\".", provider=self.name,
            )
        self.timeout = float(self.options.get("timeout", 30))

    def can_handle(self, url: str) -> bool:
        return parse_url(url) is not None

    # --------------------------------------------------------------- fallback
    def api_fallback_allowed(self) -> bool:
        """Solo con ``mode = "spotify_api"`` esplicito E credenziali in ambiente (.env)."""
        return (self.mode == MODE_API
                and bool((os.environ.get("SPOTIFY_CLIENT_ID") or "").strip())
                and bool((os.environ.get("SPOTIFY_CLIENT_SECRET") or "").strip()))

    def _resolve_via_api(self, url: str, cause: ProviderError) -> list[Track]:
        from core import registry  # import a runtime: il registry importa i provider

        api = registry.load("spotify")
        if api is None:
            cause.hint = (f"{cause.hint or ''} Il fallback sulla Web API non è disponibile: "
                          f"{registry.UNAVAILABLE.get('spotify', 'provider spotify non caricato')}").strip()
            raise cause
        log.warning("[spotify_embed] pagina embed non utilizzabile (%s): uso la Web API (mode = spotify_api)", cause.message)
        return api.resolve(url)

    # ---------------------------------------------------------------- resolve
    def resolve(self, url: str) -> list[Track]:
        parsed = parse_url(url)
        if parsed is None:
            raise ProviderError(
                f"URL Spotify non riconosciuto: {url}", provider=self.name,
                hint="Formati accettati: https://open.spotify.com/{track|album|playlist}/<id>.",
            )
        kind, sid = parsed
        source_url = canonical_url(kind, sid)
        try:
            try:
                page = fetch_embed(EMBED_URL.format(kind=kind, sid=sid), timeout=self.timeout)
            except (urllib.error.URLError, OSError) as exc:
                raise _http_error(exc, f"pagina embed di {source_url}") from None
            entity = extract_entity(page, source_url)
            tracks = tracks_from_entity(entity, source_url, self.name)
            if not tracks:
                raise ProviderError(f"la pagina embed non elenca tracce per {source_url}", provider=self.name,
                                    kind="missing", hint="Controlla che il link sia pubblico e non vuoto.")
            return tracks
        except ProviderError as exc:
            if exc.retryable or not self.api_fallback_allowed():
                raise
            return self._resolve_via_api(url, exc)
