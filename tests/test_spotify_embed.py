"""Provider spotify_embed: fixture HTML reali (pagina embed, solo ``entity``,
senza token di sessione) salvate il 2026-11-10. Nessuna rete."""

from __future__ import annotations

import json
import re
import urllib.error
from pathlib import Path

import pytest

from core import registry
from core.config import Config, PROJECT_ROOT
from core.pipeline import Pipeline
from sources import spotify_embed as se
from sources.base import ProviderError, ProviderUnavailable, Track
from sources.spotify_embed import SpotifyEmbedSource, extract_entity, parse_url, split_artists, tracks_from_entity

FIXTURES = Path(__file__).parent / "fixtures"
PLAYLIST_ID = "6NrlbBDMMeZ9Mot011Cfqu"
ALBUM_ID = "4aawyAB9vmqN3uQ7FjRGTy"
TRACK_ID = "4cOdK2wGLETKBW3PvgPWqT"


def page(kind: str) -> str:
    return (FIXTURES / f"spotify_embed_{kind}.html").read_text(encoding="utf-8")


def rebuild(entity: dict) -> str:
    """Pagina embed minima attorno a un ``entity`` modificato."""
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'


@pytest.fixture
def served(monkeypatch):
    """Sostituisce il GET della pagina embed: ``pages[url] -> html | eccezione``."""
    pages: dict[str, object] = {}
    calls: list[str] = []

    def fetch(url, *, timeout=30.0):
        calls.append(url)
        if url not in pages:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        value = pages[url]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(se, "fetch_embed", fetch)
    return pages, calls


# ------------------------------------------------------------------ URL
@pytest.mark.parametrize("url, expected", [
    (f"https://open.spotify.com/track/{TRACK_ID}", ("track", TRACK_ID)),
    (f"https://open.spotify.com/intl-it/album/{ALBUM_ID}?si=abc", ("album", ALBUM_ID)),
    (f"https://open.spotify.com/playlist/{PLAYLIST_ID}/", ("playlist", PLAYLIST_ID)),
    (f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}", ("playlist", PLAYLIST_ID)),
    (f"spotify:album:{ALBUM_ID}", ("album", ALBUM_ID)),
    ("https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi", None),   # artist non è gestito qui
    ("https://open.spotify.com/show/4tZwfgrHOc3mvqYlEYSvVi", None),
    ("https://open.spotify.com/", None),
    ("https://soundcloud.com/forss/flickermood", None),
    ("https://open.spotify.com/track/short", None),
])
def test_parse_url_and_can_handle(url: str, expected):
    assert parse_url(url) == expected
    assert SpotifyEmbedSource().can_handle(url) is (expected is not None)


def test_provider_contract():
    src = SpotifyEmbedSource()
    assert src.name == "spotify_embed" and src.supports_direct_audio is False and src.accepts_delegation is True
    assert src.mode == "embed" and src.audio_provider is None
    assert SpotifyEmbedSource({"audio_provider": "youtube"}).audio_provider == "youtube"
    with pytest.raises(ProviderUnavailable, match="non riconosciuto"):
        SpotifyEmbedSource({"mode": "magic"})


# -------------------------------------------------------------- fixture
def test_fixtures_are_real_and_carry_no_session_token():
    for kind in ("playlist", "album", "track"):
        text = page(kind)
        assert '<script id="__NEXT_DATA__" type="application/json">' in text
        for secret in ("accessToken", "clientId", "session"):
            assert secret not in text, f"{kind}: la fixture non deve contenere {secret}"
        assert extract_entity(text, "x")["type"] == kind


# -------------------------------------------------------------- mapping
def test_playlist_tracklist_is_mapped():
    entity = extract_entity(page("playlist"), "x")
    url = f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
    tracks = tracks_from_entity(entity, url)
    assert len(tracks) == len(entity["trackList"]) == 48
    first = tracks[0]
    assert first.provider == "spotify_embed" and first.provider_id == "0BK6ACxpzK8nkWa3YKuunX"
    assert first.title == "Fancy Footwork" and first.artists == ["Chris Lorenzo", "Chromeo"]
    assert first.duration_ms == 192195
    # niente campi inventati: l'immagine è della PLAYLIST, non del brano
    assert first.album is None and first.track_no is None and first.year is None and first.isrc is None
    assert first.cover_url is None and first.extra["playlist_cover_url"].startswith("https://image-cdn")
    assert first.extra["playlist"] == "House Music is back 2026" and first.extra["playlist_owner"] == "Max Niklas"
    assert first.extra["playlist_index"] == 1 and tracks[-1].extra["playlist_index"] == 48
    assert first.extra["url"] == "https://open.spotify.com/track/0BK6ACxpzK8nkWa3YKuunX"
    assert first.extra["source_url"] == url and first.extra["spotify_uri"] == "spotify:track:0BK6ACxpzK8nkWa3YKuunX"
    assert tracks[-1].title == "Fight For Love" and tracks[-1].artists == ["Alesso", "Kaitlin Aragon"]
    assert len({t.provider_id for t in tracks}) == 48 and all(t.duration_ms and t.artists for t in tracks)


def test_album_tracklist_is_mapped_with_cover_and_numbers():
    entity = extract_entity(page("album"), "x")
    tracks = tracks_from_entity(entity, f"https://open.spotify.com/album/{ALBUM_ID}")
    assert len(tracks) == 18 and [t.track_no for t in tracks] == list(range(1, 19))
    assert all(t.album == "Global Warming" and t.extra["albumartist"] == "Pitbull" for t in tracks)
    first = tracks[0]
    assert first.title == "Global Warming (feat. Sensato)" and first.artists == ["Pitbull", "Sensato"]
    assert first.duration_ms == 85400 and first.extra["explicit"] is True
    largest = max(entity["visualIdentity"]["image"], key=lambda i: i["maxWidth"] * i["maxHeight"])
    assert largest["maxWidth"] == 640 and all(t.cover_url == largest["url"] for t in tracks)
    assert all(t.year is None for t in tracks)  # la pagina embed dell'album non riporta la data
    assert tracks[-1].artists == ["Pitbull", "Chris Brown", "Jump Smokers"]
    assert "playlist" not in first.extra


def test_single_track_is_mapped():
    tracks = tracks_from_entity(extract_entity(page("track"), "x"), f"https://open.spotify.com/track/{TRACK_ID}")
    assert len(tracks) == 1
    t = tracks[0]
    assert t.provider_id == TRACK_ID and t.title == "Never Gonna Give You Up" and t.artists == ["Rick Astley"]
    assert t.duration_ms == 213573 and t.year == 1987 and t.cover_url.startswith("https://image-cdn")
    assert t.album is None and t.track_no is None  # non presenti nella pagina embed della traccia


def test_items_without_uri_are_skipped_and_positions_kept():
    entity = extract_entity(page("album"), "x")
    entity["trackList"][1]["uri"] = None
    del entity["trackList"][2]["uri"]
    entity["trackList"][3]["uri"] = "spotify:episode:4cOdK2wGLETKBW3PvgPWqT"  # non è una traccia
    entity["trackList"][4] = None
    tracks = tracks_from_entity(entity, "x")
    assert len(tracks) == 14 and [t.track_no for t in tracks][:3] == [1, 6, 7]


def test_split_artists_keeps_names_with_plain_commas():
    assert split_artists("Chris Lorenzo, Chromeo") == ["Chris Lorenzo", "Chromeo"]
    assert split_artists("Tyler, The Creator") == ["Tyler, The Creator"]
    assert split_artists("Tyler, The Creator, Kali Uchis") == ["Tyler, The Creator", "Kali Uchis"]
    assert split_artists(None) == [] and split_artists("") == []


# ------------------------------------------------- __NEXT_DATA__ mancante
def test_missing_next_data_fails_with_actionable_message():
    with pytest.raises(ProviderError) as exc:
        extract_entity("<html><head><title>Spotify</title></head><body>niente dati</body></html>",
                       f"https://open.spotify.com/playlist/{PLAYLIST_ID}")
    err = exc.value
    assert err.kind == "layout_changed" and err.retryable is False and err.provider == "spotify_embed"
    assert "__NEXT_DATA__" in err.message and PLAYLIST_ID in err.message
    for needle in ('mode = "spotify_api"', "[options.spotify_embed]", "SPOTIFY_CLIENT_ID", ".env", "Premium"):
        assert needle in err.hint, needle


def test_broken_next_data_variants_are_actionable():
    with pytest.raises(ProviderError, match="non è JSON valido") as exc:
        extract_entity('<script id="__NEXT_DATA__" type="application/json">{not json</script>', "u")
    assert exc.value.kind == "layout_changed"
    with pytest.raises(ProviderError, match="state.data.entity") as exc:
        extract_entity('<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{}}}</script>', "u")
    assert exc.value.kind == "layout_changed" and "spotify_api" in exc.value.hint
    # attributi in ordine diverso: lo script viene comunque trovato
    alt = page("track").replace('<script id="__NEXT_DATA__" type="application/json">',
                                '<script type="application/json" id="__NEXT_DATA__" crossorigin="anonymous">')
    assert extract_entity(alt, "u")["type"] == "track"


# -------------------------------------------------------------- resolve
def test_resolve_fetches_the_embed_url_of_the_canonical_id(served):
    pages, calls = served
    pages[f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}"] = page("playlist")
    pages[f"https://open.spotify.com/embed/track/{TRACK_ID}"] = page("track")
    src = SpotifyEmbedSource()
    tracks = src.resolve(f"https://open.spotify.com/intl-it/playlist/{PLAYLIST_ID}?si=abc123")
    assert len(tracks) == 48 and calls == [f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}"]
    assert tracks[0].extra["source_url"] == f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
    assert len(src.resolve(f"spotify:track:{TRACK_ID}")) == 1
    with pytest.raises(ProviderError, match="non riconosciuto"):
        src.resolve("https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi")


def test_resolve_http_errors_are_classified(served):
    pages, _ = served
    src = SpotifyEmbedSource()
    with pytest.raises(ProviderError) as exc:
        src.resolve(f"https://open.spotify.com/album/{ALBUM_ID}")  # 404 dal mock
    assert exc.value.kind == "missing" and exc.value.status_code == 404 and "private" in exc.value.hint
    url = f"https://open.spotify.com/embed/album/{ALBUM_ID}"
    pages[url] = urllib.error.HTTPError(url, 429, "Too Many", {}, None)
    with pytest.raises(ProviderError) as exc:
        src.resolve(f"https://open.spotify.com/album/{ALBUM_ID}")
    assert exc.value.retryable and exc.value.status_code == 429
    pages[url] = urllib.error.URLError("timed out")
    with pytest.raises(ProviderError) as exc:
        src.resolve(f"https://open.spotify.com/album/{ALBUM_ID}")
    assert exc.value.retryable and exc.value.kind == "network"
    entity = extract_entity(page("album"), "x")
    entity["trackList"] = []
    pages[url] = rebuild(entity)
    with pytest.raises(ProviderError, match="non elenca tracce"):
        src.resolve(f"https://open.spotify.com/album/{ALBUM_ID}")


# ------------------------------------------------------------- fallback
class _FakeApi:
    name = "spotify"

    def __init__(self):
        self.calls: list[str] = []

    def resolve(self, url):
        self.calls.append(url)
        return [Track("spotify", "apiid", "From API", ["A"])]


def test_api_fallback_only_with_explicit_mode_and_credentials(served, monkeypatch):
    pages, _ = served
    url = f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
    pages[f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}"] = "<html>pagina cambiata, niente __NEXT_DATA</html>"
    api = _FakeApi()
    monkeypatch.setattr(registry, "load", lambda name: api if name == "spotify" else None)

    # 1. credenziali presenti ma modalità di default: NESSUN fallback
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "sec")
    src = SpotifyEmbedSource()
    assert src.api_fallback_allowed() is False
    with pytest.raises(ProviderError) as exc:
        src.resolve(url)
    assert exc.value.kind == "layout_changed" and api.calls == []

    # 2. modalità esplicita ma credenziali assenti: NESSUN fallback
    monkeypatch.delenv("SPOTIFY_CLIENT_ID")
    src = SpotifyEmbedSource({"mode": "spotify_api"})
    assert src.api_fallback_allowed() is False
    with pytest.raises(ProviderError):
        src.resolve(url)
    assert api.calls == []
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "  ")  # vuota di fatto
    assert SpotifyEmbedSource({"mode": "spotify_api"}).api_fallback_allowed() is False

    # 3. modalità esplicita E credenziali: fallback sulla Web API
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    src = SpotifyEmbedSource({"mode": "spotify_api"})
    assert src.api_fallback_allowed() is True
    tracks = src.resolve(url)
    assert [t.title for t in tracks] == ["From API"] and api.calls == [url]

    # 4. con la pagina embed leggibile la Web API non viene toccata nemmeno in spotify_api
    pages[f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}"] = page("playlist")
    api.calls.clear()
    assert len(src.resolve(url)) == 48 and api.calls == []

    # 5. un errore temporaneo (429) non fa scattare il fallback: lo ritenta il core
    eurl = f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}"
    pages[eurl] = urllib.error.HTTPError(eurl, 429, "Too Many", {}, None)
    with pytest.raises(ProviderError) as exc:
        src.resolve(url)
    assert exc.value.retryable and api.calls == []


def test_api_fallback_unavailable_keeps_the_original_error(served, monkeypatch):
    pages, _ = served
    pages[f"https://open.spotify.com/embed/track/{TRACK_ID}"] = "<html></html>"
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "sec")
    monkeypatch.setattr(registry, "load", lambda name: None)
    registry.UNAVAILABLE["spotify"] = "spotipy non installato"
    with pytest.raises(ProviderError) as exc:
        SpotifyEmbedSource({"mode": "spotify_api"}).resolve(f"https://open.spotify.com/track/{TRACK_ID}")
    assert exc.value.kind == "layout_changed" and "spotipy non installato" in exc.value.hint


# ------------------------------------------------ dispatch e pipeline
def test_dispatch_prefers_embed_with_project_priority(monkeypatch, tmp_path: Path):
    """Con le credenziali presenti il provider della Web API si carica, ma con la
    priorità di config.toml i link Spotify vanno alla pagina embed."""
    from core import config as config_mod

    cfg = config_mod.load(PROJECT_ROOT / "config.toml")
    assert cfg.priority[:2] == ["spotify_embed", "spotify"]
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "sec")
    registry.configure({"spotify": {"cache_path": str(tmp_path / "c.sqlite3"), "unmatched_log_file": str(tmp_path / "u.log"),
                                    "token_cache_path": str(tmp_path / "t.json")}}, cfg.priority)
    assert registry.load("spotify") is not None  # disponibile, ma non scelto
    for url in (f"https://open.spotify.com/playlist/{PLAYLIST_ID}", f"https://open.spotify.com/album/{ALBUM_ID}"):
        assert registry.dispatch(url, order=cfg.priority).name == "spotify_embed"
    # gli URL artista restano alla Web API (la pagina embed non li copre)
    assert registry.dispatch("https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi", order=cfg.priority).name == "spotify"
    assert registry.dispatch("https://x", forced="spotify").name == "spotify"


def test_pipeline_end_to_end_with_embed_and_delegated_audio(fake_sources, served, tmp_path: Path):
    pages, _ = served
    pages[f"https://open.spotify.com/embed/album/{ALBUM_ID}"] = page("album")
    registry.configure({"spotify_embed": {"audio_provider": "fakeok"}}, ["spotify_embed", "fakeok"])
    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["spotify_embed", "fakeok"])
    p = Pipeline(cfg)
    summary = p.run([f"https://open.spotify.com/album/{ALBUM_ID}"])
    p.close()
    assert summary.ok and len(summary.done) == 18
    assert summary.done[0].source == "spotify_embed" and summary.done[0].audio_provider == "fakeok"
    names = sorted(f.name for f in (tmp_path / "out" / "Pitbull, Sensato" / "Global Warming").iterdir())
    assert names == ["01 - Global Warming (feat. Sensato).ogg"]
    assert len(list((tmp_path / "out").rglob("*.ogg"))) == 18


def test_module_never_reads_the_session_token():
    source = Path(se.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)  # fuori dai docstring
    assert "accessToken" not in code and "settings" not in code and "Authorization" not in code
