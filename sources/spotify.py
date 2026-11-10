"""Provider Spotify in sola modalità metadati.

Risolve track, album, playlist e top track di un artista tramite la Web API
ufficiale (spotipy) e delega l'audio a un provider con ricerca
(``audio_provider``). Non esiste una modalità "direct": da Spotify arrivano
solo metadati.

Autenticazione: ``client_credentials`` (default, catalogo pubblico) oppure
``pkce`` (playlist private e brani salvati, token in cache su disco).
Credenziali da .env: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

from core.retry import call_with_retry
from sources.base import (
    MetadataSource,
    NoMatch,
    ProgressCallback,
    ProviderError,
    ProviderUnavailable,
    Track,
)

try:
    import requests
    import spotipy
    from spotipy.cache_handler import CacheFileHandler, MemoryCacheHandler
    from spotipy.exceptions import SpotifyException
    from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOauthError, SpotifyPKCE
except ImportError:  # pragma: no cover - dipende dall'ambiente
    spotipy = None  # type: ignore[assignment]

    class SpotifyException(Exception):  # type: ignore[no-redef]
        pass

    class SpotifyOauthError(Exception):  # type: ignore[no-redef]
        pass

log = logging.getLogger("grabmedia.spotify")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_URL = "https://developer.spotify.com/dashboard"
PKCE_SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
HOSTS = {"open.spotify.com", "play.spotify.com"}
KINDS = ("track", "album", "playlist", "artist")
PAGE_PLAYLIST = 100
PAGE_ALBUM = 50
ISRC_BATCH = 50
DEFAULT_CACHE_TTL_DAYS = 7
DEFAULT_UNMATCHED_THRESHOLD = 0.72
_NO_STATUS_RETRY = (0,)  # status HTTP impossibile: disattiva i retry interni di spotipy/urllib3

_PATH_RE = re.compile(r"^/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(track|album|playlist|artist)/([A-Za-z0-9]{22})/?$")
_URI_RE = re.compile(r"^spotify:(track|album|playlist|artist):([A-Za-z0-9]{22})$")

_HINT_DASHBOARD = (
    f"Crea un'app su {DASHBOARD_URL}, copia Client ID e Client secret in .env "
    "(vedi .env.example) e rilancia."
)
_HINT_403 = (
    "L'app Spotify è in Development Mode: può essere usata solo dagli account elencati in "
    f"{DASHBOARD_URL} -> la tua app -> Settings -> User Management. Per playlist e contenuti "
    "editoriali Spotify richiede inoltre l'Extended Quota Mode (richiedila dalla dashboard). "
    "In alternativa aggiungi il tuo account alla lista utenti e usa auth = \"pkce\" in [options.spotify]."
)


# ------------------------------------------------------------------ URL
def parse_url(url: str) -> tuple[str, str] | None:
    """``(kind, id)`` per URL open.spotify.com o URI ``spotify:kind:id``."""
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
    if not m:
        return None
    return m.group(1), m.group(2)


def canonical_url(kind: str, sid: str) -> str:
    return f"https://open.spotify.com/{kind}/{sid}"


# --------------------------------------------------------------- errori
def map_error(exc: BaseException, provider: str, *, what: str = "") -> ProviderError:
    """``SpotifyException`` -> ``ProviderError``: 429 retryable con
    ``retry_after``, 5xx retryable, 401/403 ``ProviderUnavailable`` con hint."""
    status = getattr(exc, "http_status", None)
    headers = getattr(exc, "headers", None) or {}
    msg = getattr(exc, "msg", None) or str(exc)
    msg = str(msg).strip().splitlines()[-1].strip() if msg else type(exc).__name__
    prefix = f"{what}: " if what else ""
    if status == 429:
        err = ProviderError(f"{prefix}rate limit Spotify", retryable=True, status_code=429, provider=provider,
                            hint="Il core riprova rispettando Retry-After.")
        retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
        if retry_after is not None:
            try:
                err.retry_after = float(retry_after)  # type: ignore[attr-defined]
            except (TypeError, ValueError):
                pass
        return err
    if status is not None and 500 <= status < 600:
        return ProviderError(f"{prefix}errore del server Spotify ({msg})", retryable=True,
                             status_code=status, provider=provider, hint="Errore temporaneo: il core riprova con backoff.")
    if status == 401:
        return ProviderUnavailable(f"{prefix}credenziali rifiutate (401): {msg}", provider=provider, hint=_HINT_DASHBOARD)
    if status == 403:
        return ProviderUnavailable(f"{prefix}accesso negato (403): {msg}", provider=provider, hint=_HINT_403)
    if status == 404:
        return ProviderError(f"{prefix}non trovato (404): {msg}", status_code=404, provider=provider,
                             hint="Controlla l'URL. Con auth = \"client_credentials\" le playlist private non sono visibili: usa \"pkce\".")
    if status == 400:
        return ProviderError(f"{prefix}richiesta rifiutata (400): {msg}", status_code=400, provider=provider,
                             hint="ID non valido o market non riconosciuto in [options.spotify].")
    return ProviderError(f"{prefix}{msg}", status_code=status, provider=provider)


# ---------------------------------------------------------------- cache
class MetadataCache:
    """Cache su SQLite delle tracce risolte, chiave = URL canonico, con TTL."""

    def __init__(self, path: Path | str, ttl_seconds: float) -> None:
        self.path = Path(path)
        self.ttl = float(ttl_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)"
            )

    def get(self, key: str) -> list[Track] | None:
        with self._lock:
            row = self._conn.execute("SELECT payload, created_at FROM cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            payload, created_at = row
            if time.time() - created_at > self.ttl:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                return None
        try:
            return [Track(**item) for item in json.loads(payload)]
        except (TypeError, ValueError, json.JSONDecodeError):
            log.warning("Cache Spotify corrotta per %s: ignorata", key)
            return None

    def put(self, key: str, tracks: Iterable[Track]) -> None:
        payload = json.dumps([asdict(t) for t in tracks], ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, payload, created_at) VALUES (?, ?, ?)",
                (key, payload, time.time()),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# -------------------------------------------------------------- provider
class SpotifySource(MetadataSource):
    name = "spotify"
    supports_direct_audio = False
    accepts_delegation = True

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        if spotipy is None:
            raise ProviderUnavailable("il pacchetto spotipy non è installato", hint="Esegui: pip install -U spotipy", provider=self.name)
        o = self.options
        self.audio_provider = (o.get("audio_provider") or None)
        self.auth_mode = str(o.get("auth") or "client_credentials").strip().lower()
        self.market: str | None = (str(o["market"]).strip().upper() or None) if o.get("market") else None
        self.fetch_isrc = bool(o.get("fetch_isrc", False))
        self.unmatched_threshold = float(o.get("unmatched_threshold", DEFAULT_UNMATCHED_THRESHOLD))
        self.unmatched_log = Path(o.get("unmatched_log_file") or PROJECT_ROOT / "downloads" / "unmatched.log")
        self.timeout = float(o.get("timeout", 15))
        self.max_attempts = max(1, int(o.get("max_attempts", 4)))
        ttl_days = float(o.get("cache_ttl_days", DEFAULT_CACHE_TTL_DAYS))
        cache_path = Path(o.get("cache_path") or PROJECT_ROOT / "downloads" / "spotify_cache.sqlite3")
        self.token_cache_path = Path(o.get("token_cache_path") or PROJECT_ROOT / "downloads" / ".spotify_token.json")
        self._sleep: Callable[[float], None] = time.sleep
        self._user_market_checked = False
        self._log_lock = threading.Lock()
        self._sp = self._build_client()
        self._cache = MetadataCache(cache_path, ttl_days * 86400)

    # ---------------------------------------------------------------- auth
    def _build_client(self):
        client_id = (os.environ.get("SPOTIFY_CLIENT_ID") or "").strip()
        client_secret = (os.environ.get("SPOTIFY_CLIENT_SECRET") or "").strip()
        redirect_uri = (os.environ.get("SPOTIFY_REDIRECT_URI") or "").strip()
        if not client_id:
            raise ProviderUnavailable("SPOTIFY_CLIENT_ID mancante in .env", hint=_HINT_DASHBOARD, provider=self.name)
        if self.auth_mode == "pkce":
            if not redirect_uri:
                raise ProviderUnavailable(
                    "SPOTIFY_REDIRECT_URI mancante in .env (richiesto da auth = \"pkce\")",
                    hint="Imposta lo stesso Redirect URI registrato nella dashboard, es. http://127.0.0.1:8888/callback.",
                    provider=self.name,
                )
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            auth = SpotifyPKCE(
                client_id=client_id,
                redirect_uri=redirect_uri,
                scope=PKCE_SCOPES,
                cache_handler=CacheFileHandler(cache_path=str(self.token_cache_path)),
                open_browser=True,
                requests_timeout=self.timeout,
            )
        elif self.auth_mode == "client_credentials":
            if not client_secret:
                raise ProviderUnavailable("SPOTIFY_CLIENT_SECRET mancante in .env", hint=_HINT_DASHBOARD, provider=self.name)
            auth = SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
                cache_handler=MemoryCacheHandler(),
                requests_timeout=self.timeout,
            )
        else:
            raise ProviderUnavailable(
                f"auth = \"{self.auth_mode}\" non riconosciuto in [options.spotify]",
                hint="Valori validi: \"client_credentials\" oppure \"pkce\".",
                provider=self.name,
            )
        # Niente retry interni: li fa il core (così 429 e 5xx arrivano qui con
        # gli header, Retry-After compreso, invece di un generico "Max Retries").
        # spotipy fa ``status_forcelist or default_retry_codes``: una tupla vuota
        # riattiverebbe i default, quindi passiamo uno status impossibile.
        return spotipy.Spotify(
            auth_manager=auth,
            retries=0,
            status_retries=0,
            status_forcelist=_NO_STATUS_RETRY,
            backoff_factor=0,
            requests_timeout=self.timeout,
        )

    def _call(self, what: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Una chiamata API con retry su 429 (Retry-After) e 5xx."""

        def attempt() -> Any:
            try:
                return fn(*args, **kwargs)
            except SpotifyException as exc:
                raise map_error(exc, self.name, what=what) from None
            except SpotifyOauthError as exc:
                raise ProviderUnavailable(f"autenticazione Spotify fallita: {exc}", hint=_HINT_DASHBOARD, provider=self.name) from None
            except requests.RequestException as exc:
                raise ProviderError(f"{what}: errore di rete ({exc})", retryable=True, provider=self.name,
                                    hint="Controlla la connessione; il core riprova con backoff.") from None

        return call_with_retry(attempt, provider=self.name, max_attempts=self.max_attempts, sleep=self._sleep, what=what)

    def _effective_market(self) -> str | None:
        if self.market:
            return self.market
        if self.auth_mode == "pkce" and not self._user_market_checked:
            self._user_market_checked = True
            try:
                me = self._call("profilo utente", self._sp.me)
                country = (me or {}).get("country")
                if country:
                    self.market = str(country).upper()
            except ProviderError as exc:
                log.debug("market dell'utente non disponibile: %s", exc)
        return self.market

    # ------------------------------------------------------------- contratto
    def can_handle(self, url: str) -> bool:
        return parse_url(url) is not None

    def resolve(self, url: str) -> list[Track]:
        parsed = parse_url(url)
        if parsed is None:
            raise ProviderError(
                f"URL Spotify non riconosciuto: {url}",
                hint="Formati accettati: https://open.spotify.com/{track|album|playlist|artist}/<id> o spotify:<tipo>:<id>.",
                provider=self.name,
            )
        kind, sid = parsed
        key = canonical_url(kind, sid)
        cached = self._cache.get(key)
        if cached is not None:
            log.info("[spotify] %d tracce dalla cache per %s", len(cached), key)
            return cached

        market = self._effective_market()
        resolver = {
            "track": self._resolve_track,
            "album": self._resolve_album,
            "playlist": self._resolve_playlist,
            "artist": self._resolve_artist,
        }[kind]
        tracks = self._dedupe(resolver(sid, key, market))
        if self.fetch_isrc:
            self._fill_isrc(tracks, market)
        self._cache.put(key, tracks)
        return tracks

    def download(
        self,
        track: Track,
        dest: Path,
        quality: str,
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        # Il provider audio non deve scaricare sotto la soglia: gliela passiamo.
        current = track.extra.get("match_threshold")
        track.extra["match_threshold"] = max(float(current or 0.0), self.unmatched_threshold)
        try:
            result = super().download(track, dest, quality, progress_cb)
        except NoMatch as exc:
            self._log_unmatched(track, exc)
            raise
        score = track.extra.get("match_score")
        if score is not None and float(score) < self.unmatched_threshold:
            log.warning("[spotify] '%s' scaricata con punteggio %.2f sotto la soglia %.2f",
                        track.display(), float(score), self.unmatched_threshold)
        return result

    def close(self) -> None:
        try:
            self._cache.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("close() cache: %s", exc)
        session = getattr(self._sp, "_session", None)
        if session is not None and hasattr(session, "close"):
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("close() sessione: %s", exc)

    # -------------------------------------------------------------- resolve
    def _paginate(self, what: str, fetch: Callable[[int], dict], page_size: int) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while True:
            page = self._call(f"{what} (offset {offset})", fetch, offset) or {}
            batch = page.get("items") or []
            items.extend(batch)
            total = page.get("total")
            offset += page_size
            if not page.get("next") or not batch or (isinstance(total, int) and offset >= total):
                break
        return items

    def _resolve_track(self, sid: str, key: str, market: str | None) -> list[Track]:
        t = self._call("traccia", self._sp.track, sid, market=market)
        return [self._track_from(t, album=t.get("album"), source_url=key)]

    def _resolve_album(self, sid: str, key: str, market: str | None) -> list[Track]:
        album = self._call("album", self._sp.album, sid, market=market)
        items = self._paginate(
            "tracce dell'album",
            lambda offset: self._sp.album_tracks(sid, limit=PAGE_ALBUM, offset=offset, market=market),
            PAGE_ALBUM,
        )
        return [self._track_from(t, album=album, source_url=key) for t in items if self._usable(t)]

    def _resolve_playlist(self, sid: str, key: str, market: str | None) -> list[Track]:
        meta = self._call("playlist", self._sp.playlist, sid, fields="name,owner.display_name", market=market) or {}
        items = self._paginate(
            "elementi della playlist",
            lambda offset: self._sp.playlist_items(
                sid, limit=PAGE_PLAYLIST, offset=offset, market=market, additional_types=("track",)
            ),
            PAGE_PLAYLIST,
        )
        extra = {"playlist": meta.get("name"), "playlist_owner": (meta.get("owner") or {}).get("display_name")}
        tracks: list[Track] = []
        for index, item in enumerate(items, 1):
            if not item or item.get("is_local"):
                continue
            t = item.get("track") or item.get("item")
            if not self._usable(t):
                continue
            tracks.append(self._track_from(t, album=t.get("album"), source_url=key, extra={**extra, "playlist_index": index}))
        return tracks

    def _resolve_artist(self, sid: str, key: str, market: str | None) -> list[Track]:
        top = self._call("top track dell'artista", self._sp.artist_top_tracks, sid, country=market or "US") or {}
        return [self._track_from(t, album=t.get("album"), source_url=key) for t in top.get("tracks") or [] if self._usable(t)]

    @staticmethod
    def _usable(t: Mapping[str, Any] | None) -> bool:
        if not t or not t.get("id"):
            return False
        if t.get("is_local"):
            return False
        return t.get("type") in (None, "track")

    @staticmethod
    def _dedupe(tracks: Iterable[Track]) -> list[Track]:
        seen: set[str] = set()
        out: list[Track] = []
        for t in tracks:
            if t.provider_id in seen:
                continue
            seen.add(t.provider_id)
            out.append(t)
        return out

    def _fill_isrc(self, tracks: list[Track], market: str | None) -> None:
        by_id = {t.provider_id: t for t in tracks}
        ids = list(by_id)
        for start in range(0, len(ids), ISRC_BATCH):
            batch = ids[start:start + ISRC_BATCH]
            data = self._call(f"ISRC (lotto {start // ISRC_BATCH + 1})", self._sp.tracks, batch, market=market) or {}
            for full in data.get("tracks") or []:
                if not full or not full.get("id"):
                    continue
                isrc = (full.get("external_ids") or {}).get("isrc")
                if isrc and full["id"] in by_id:
                    by_id[full["id"]].isrc = str(isrc).strip().upper()

    @staticmethod
    def _year(release_date: Any) -> int | None:
        if not release_date:
            return None
        m = re.match(r"^(\d{4})", str(release_date))
        return int(m.group(1)) if m else None

    def _track_from(
        self,
        t: Mapping[str, Any],
        *,
        album: Mapping[str, Any] | None,
        source_url: str,
        extra: Mapping[str, Any] | None = None,
    ) -> Track:
        album = album or {}
        images = [i for i in (album.get("images") or []) if i and i.get("url")]
        cover = max(images, key=lambda i: (i.get("width") or 0) * (i.get("height") or 0))["url"] if images else None
        artists = [a["name"].strip() for a in (t.get("artists") or []) if a and a.get("name")]
        album_artists = [a["name"].strip() for a in (album.get("artists") or []) if a and a.get("name")]
        merged: dict[str, Any] = {
            "url": canonical_url("track", t["id"]),
            "spotify_uri": t.get("uri"),
            "source_url": source_url,
            "albumartist": album_artists[0] if album_artists else None,
            "album_total_tracks": album.get("total_tracks"),
            "explicit": t.get("explicit"),
        }
        merged.update({k: v for k, v in (extra or {}).items() if v is not None})
        return Track(
            provider=self.name,
            provider_id=str(t["id"]),
            title=str(t.get("name") or "").strip() or "Untitled",
            artists=artists,
            album=(album.get("name") or None),
            duration_ms=int(t["duration_ms"]) if t.get("duration_ms") is not None else None,
            isrc=None,  # solo con fetch_isrc, via /tracks?ids=
            track_no=int(t["track_number"]) if t.get("track_number") else None,
            disc_no=int(t["disc_number"]) if t.get("disc_number") else None,
            year=self._year(album.get("release_date")),
            cover_url=cover,
            extra={k: v for k, v in merged.items() if v is not None},
        )

    # ------------------------------------------------------------ unmatched
    def _log_unmatched(self, track: Track, exc: NoMatch) -> None:
        query = exc.query or " ".join(p for p in (track.artist, track.title) if p)
        score = exc.score if exc.score is not None else 0.0
        line = "\t".join([
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            str(track.extra.get("url") or canonical_url("track", track.provider_id)),
            query,
            f"{score:.3f}",
            str(exc.provider or self.audio_provider or ""),
        ])
        with self._log_lock:
            self.unmatched_log.parent.mkdir(parents=True, exist_ok=True)
            with self.unmatched_log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        log.warning("[spotify] nessun match per %s: registrato in %s", track.display(), self.unmatched_log)
