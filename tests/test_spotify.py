"""Test offline del provider Spotify: le cassette vcrpy sono generate nel test
(record_mode="none" -> ogni richiesta non registrata fallisce, nessuna rete)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import vcr
import yaml

from core import registry
from core.config import Config
from core.pipeline import Pipeline
from sources import spotify as spotify_mod
from sources.base import NoMatch, ProviderError, ProviderUnavailable, Track
from sources.spotify import SpotifySource, canonical_url, map_error, parse_url

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
ALBUM_ID = "4aawyAB9vmqN3uQ7FjRGTy"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
ARTIST_ID = "4tZwfgrHOc3mvqYlEYSvVi"
TRACK_ID = "6rqhFgbbKwnb9MLmUQDhG6"

VCR_MATCH = ["method", "scheme", "host", "port", "path", "query"]


# ------------------------------------------------------------- helpers
def _tid(n: int) -> str:
    """ID Spotify finto di 22 caratteri: ``t7xxxxxxxxxxxxxxxxxxxx``."""
    return f"t{n}".ljust(22, "x")


def track_obj(n: int, *, album: dict | None = None, artist: str = "Daft Punk", is_local: bool = False, type_: str = "track") -> dict:
    t = {
        "id": _tid(n), "name": f"Song {n}", "uri": f"spotify:track:{_tid(n)}", "type": type_,
        "artists": [{"name": artist}], "duration_ms": 200_000 + n, "track_number": n, "disc_number": 1,
        "explicit": False, "is_local": is_local,
        "external_ids": {"isrc": f"GBUM7{n:07d}"},
    }
    if album is not None:
        t["album"] = album
    return t


def album_obj(n_tracks: int) -> dict:
    return {
        "id": ALBUM_ID, "name": "Discovery", "release_date": "2001-03-12", "total_tracks": n_tracks,
        "artists": [{"name": "Daft Punk"}],
        "images": [
            {"url": "https://i.scdn.co/image/small", "width": 64, "height": 64},
            {"url": "https://i.scdn.co/image/large", "width": 640, "height": 640},
            {"url": "https://i.scdn.co/image/medium", "width": 300, "height": 300},
        ],
    }


def page(items: list, offset: int, limit: int, total: int) -> dict:
    nxt = f"{API}/x?offset={offset + limit}" if offset + limit < total else None
    return {"items": items, "offset": offset, "limit": limit, "total": total, "next": nxt}


def interaction(method: str, uri: str, status: int = 200, body=None, headers: dict | None = None) -> dict:
    resp_headers = {"Content-Type": ["application/json"]}
    for k, v in (headers or {}).items():
        resp_headers[k] = [str(v)]
    return {
        "request": {"method": method, "uri": uri, "body": None, "headers": {}},
        "response": {
            "status": {"code": status, "message": "OK" if status == 200 else "ERR"},
            "headers": resp_headers,
            "body": {"string": json.dumps(body if body is not None else {})},
        },
    }


TOKEN = interaction("POST", TOKEN_URL, 200, {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})


@pytest.fixture
def cassette(tmp_path: Path):
    """``cassette([...interazioni...])`` -> context manager vcrpy in sola riproduzione."""
    counter = {"n": 0}

    def make(interactions: list[dict], *, allow_repeats: bool = False):
        counter["n"] += 1
        path = tmp_path / f"cassette{counter['n']}.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "interactions": interactions}, sort_keys=False), encoding="utf-8")
        return vcr.VCR(record_mode="none", match_on=VCR_MATCH).use_cassette(
            str(path), allow_playback_repeats=allow_repeats
        )

    return make


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("SPOTIFY_REDIRECT_URI", raising=False)


@pytest.fixture
def opts(tmp_path: Path) -> dict:
    return {
        "market": "IT",
        "audio_provider": "fakeok",
        "cache_path": str(tmp_path / "cache.sqlite3"),
        "unmatched_log_file": str(tmp_path / "unmatched.log"),
        "token_cache_path": str(tmp_path / "token.json"),
    }


@pytest.fixture
def sp(creds, opts) -> SpotifySource:
    src = SpotifySource(opts)
    src._sleep = lambda s: None
    yield src
    src.close()


# ----------------------------------------------------------- URL / init
@pytest.mark.parametrize("url, expected", [
    (f"https://open.spotify.com/track/{TRACK_ID}", ("track", TRACK_ID)),
    (f"https://open.spotify.com/intl-it/album/{ALBUM_ID}?si=abc", ("album", ALBUM_ID)),
    (f"https://open.spotify.com/playlist/{PLAYLIST_ID}/", ("playlist", PLAYLIST_ID)),
    (f"https://open.spotify.com/artist/{ARTIST_ID}", ("artist", ARTIST_ID)),
    (f"spotify:album:{ALBUM_ID}", ("album", ALBUM_ID)),
    (f"https://open.spotify.com/show/{ALBUM_ID}", None),
    (f"https://open.spotify.com/episode/{ALBUM_ID}", None),
    ("https://open.spotify.com/", None),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
    ("https://open.spotify.com/track/short", None),
])
def test_parse_and_can_handle(sp: SpotifySource, url: str, expected):
    assert parse_url(url) == expected
    assert sp.can_handle(url) is (expected is not None)


def test_init_without_credentials(monkeypatch, opts):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    with pytest.raises(ProviderUnavailable) as exc:
        SpotifySource(opts)
    assert "SPOTIFY_CLIENT_ID" in str(exc.value) and "developer.spotify.com/dashboard" in str(exc.value)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    with pytest.raises(ProviderUnavailable, match="SPOTIFY_CLIENT_SECRET"):
        SpotifySource(opts)
    with pytest.raises(ProviderUnavailable, match="SPOTIFY_REDIRECT_URI"):
        SpotifySource({**opts, "auth": "pkce"})
    with pytest.raises(ProviderUnavailable, match="non riconosciuto"):
        SpotifySource({**opts, "auth": "implicit"})


def test_pkce_auth_manager_scopes_and_cache(monkeypatch, opts, tmp_path):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    src = SpotifySource({**opts, "auth": "pkce"})
    am = src._sp.auth_manager
    assert type(am).__name__ == "SpotifyPKCE"
    assert set(am.scope.split()) == {"playlist-read-private", "playlist-read-collaborative", "user-library-read"}
    assert am.cache_handler.cache_path == str(tmp_path / "token.json")
    assert src._sp.auth_manager.redirect_uri == "http://127.0.0.1:8888/callback"
    src.close()


def test_registry_marks_unavailable_without_credentials(monkeypatch, opts):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    registry.configure({"spotify": opts})
    assert registry.load("spotify") is None
    assert "SPOTIFY_CLIENT_ID" in registry.UNAVAILABLE["spotify"]


# --------------------------------------------------------------- resolve
def test_resolve_album(sp: SpotifySource, cassette):
    album = album_obj(2)
    with cassette([
        TOKEN,
        interaction("GET", f"{API}/albums/{ALBUM_ID}?market=IT", 200, album),
        interaction("GET", f"{API}/albums/{ALBUM_ID}/tracks/?limit=50&offset=0&market=IT", 200,
                    page([track_obj(1), track_obj(2)], 0, 50, 2)),
    ]):
        tracks = sp.resolve(f"https://open.spotify.com/intl-it/album/{ALBUM_ID}?si=xyz")
    assert [t.title for t in tracks] == ["Song 1", "Song 2"]
    t = tracks[0]
    assert t.provider == "spotify" and t.provider_id == _tid(1)
    assert t.artists == ["Daft Punk"] and t.album == "Discovery" and t.year == 2001
    assert t.track_no == 1 and t.disc_no == 1 and t.duration_ms == 200_001
    assert t.cover_url == "https://i.scdn.co/image/large"  # immagine più grande
    assert t.isrc is None  # fetch_isrc False
    assert t.extra["albumartist"] == "Daft Punk" and t.extra["url"] == canonical_url("track", _tid(1))
    assert t.extra["source_url"] == canonical_url("album", ALBUM_ID)


def test_resolve_track_and_artist(sp: SpotifySource, cassette):
    with cassette([
        TOKEN,
        interaction("GET", f"{API}/tracks/{TRACK_ID}?market=IT", 200, {**track_obj(7, album=album_obj(12)), "id": TRACK_ID}),
        interaction("GET", f"{API}/artists/{ARTIST_ID}/top-tracks?country=IT", 200,
                    {"tracks": [track_obj(1, album=album_obj(3)), track_obj(2, album=album_obj(3))]}),
    ]):
        one = sp.resolve(f"spotify:track:{TRACK_ID}")
        top = sp.resolve(f"https://open.spotify.com/artist/{ARTIST_ID}")
    assert len(one) == 1 and one[0].provider_id == TRACK_ID and one[0].album == "Discovery"
    assert [t.title for t in top] == ["Song 1", "Song 2"]


def test_resolve_playlist_paginated_skips_null_and_local_and_dedupes(sp: SpotifySource, cassette):
    album = album_obj(10)
    items_p1 = [
        {"is_local": False, "track": track_obj(1, album=album)},
        {"is_local": False, "track": None},                              # item nullo
        {"is_local": True, "track": track_obj(2, album=album, is_local=True)},  # locale
        {"is_local": False, "track": track_obj(3, album=album, type_="episode")},  # non traccia
        {"is_local": False, "track": track_obj(4, album=album)},
    ]
    items_p2 = [
        {"is_local": False, "track": track_obj(1, album=album)},         # duplicato
        {"is_local": False, "track": track_obj(5, album=album)},
        None,
    ]
    with cassette([
        TOKEN,
        interaction("GET", f"{API}/playlists/{PLAYLIST_ID}?fields=name%2Cowner.display_name&market=IT&additional_types=track",
                    200, {"name": "Today's Hits", "owner": {"display_name": "Spotify"}}),
        interaction("GET", f"{API}/playlists/{PLAYLIST_ID}/items?limit=100&offset=0&market=IT&additional_types=track",
                    200, page(items_p1, 0, 100, 108)),
        interaction("GET", f"{API}/playlists/{PLAYLIST_ID}/items?limit=100&offset=100&market=IT&additional_types=track",
                    200, page(items_p2, 100, 100, 108)),
    ]):
        tracks = sp.resolve(f"https://open.spotify.com/playlist/{PLAYLIST_ID}")
    assert [t.title for t in tracks] == ["Song 1", "Song 4", "Song 5"]
    assert tracks[0].extra["playlist"] == "Today's Hits" and tracks[0].extra["playlist_owner"] == "Spotify"
    assert tracks[2].extra["playlist_index"] == 7


def test_403_on_playlist_is_unavailable_with_hint(sp: SpotifySource, cassette):
    with cassette([
        TOKEN,
        interaction("GET", f"{API}/playlists/{PLAYLIST_ID}?fields=name%2Cowner.display_name&market=IT&additional_types=track",
                    403, {"error": {"status": 403, "message": "Forbidden"}}),
    ]):
        with pytest.raises(ProviderUnavailable) as exc:
            sp.resolve(f"https://open.spotify.com/playlist/{PLAYLIST_ID}")
    text = str(exc.value)
    assert "403" in text and "Extended Quota" in text and "User Management" in text and "pkce" in text
    assert exc.value.retryable is False


def test_real_403_premium_required_has_its_own_hint(creds, opts):
    """Cassetta REGISTRATA dal vero il 2026-11-10 (scripts/spotify_smoke.py): con
    un'app il cui proprietario non ha Premium, Spotify risponde 403 su ogni
    endpoint, con corpo in testo semplice (non JSON). L'hint deve parlare di
    Premium, non di Development Mode / User Management / Extended Quota."""
    real = Path(__file__).parent / "cassettes" / "spotify_403_premium_required.yaml"
    recorded = yaml.safe_load(real.read_text(encoding="utf-8"))["interactions"]
    # segreti oscurati alla registrazione: header di autorizzazione e access token
    assert all(i["request"]["headers"]["authorization"] == ["REDACTED"] for i in recorded)
    assert json.loads(recorded[0]["response"]["body"]["string"])["access_token"] == "REDACTED"
    assert recorded[1]["response"]["status"]["code"] == 403
    assert not recorded[1]["response"]["body"]["string"].lstrip().startswith("{")  # testo semplice, non JSON
    src = SpotifySource({k: v for k, v in opts.items() if k != "market"})  # registrata senza market
    src._sleep = lambda s: None
    try:
        with vcr.VCR(record_mode="none", match_on=VCR_MATCH).use_cassette(str(real)):
            with pytest.raises(ProviderUnavailable) as exc:
                src.resolve("https://open.spotify.com/playlist/6NrlbBDMMeZ9Mot011Cfqu")
    finally:
        src.close()
    err = exc.value
    assert err.status_code == 403 and err.kind == "premium_required" and err.retryable is False
    assert "Active premium subscription required for the owner of the app" in err.message
    assert "Premium" in err.hint and "PROPRIETARIO" in err.hint and "alcune ore" in err.hint
    assert "User Management" not in err.hint.split("Non dipende")[0]
    # il 403 generico (JSON "Forbidden") tiene l'hint su Development Mode
    from spotipy.exceptions import SpotifyException

    generic = map_error(SpotifyException(403, -1, "https://api.spotify.com/v1/x:\n Forbidden"), "spotify")
    assert generic.kind == "forbidden" and generic.status_code == 403 and "User Management" in generic.hint
    assert map_error(SpotifyException(401, -1, "x"), "spotify").status_code == 401


def test_429_respects_retry_after(sp: SpotifySource, cassette):
    sleeps: list[float] = []
    sp._sleep = sleeps.append
    with cassette([
        TOKEN,
        interaction("GET", f"{API}/tracks/{TRACK_ID}?market=IT", 429, {"error": {"status": 429, "message": "Too many"}},
                    headers={"Retry-After": "3"}),
        interaction("GET", f"{API}/tracks/{TRACK_ID}?market=IT", 200, track_obj(1, album=album_obj(1))),
    ]):
        tracks = sp.resolve(f"https://open.spotify.com/track/{TRACK_ID}")
    assert len(tracks) == 1 and sleeps == [3.0]


def test_map_error_statuses():
    from spotipy.exceptions import SpotifyException

    err = map_error(SpotifyException(429, -1, "x", headers={"Retry-After": "7"}), "spotify")
    assert err.retryable and err.status_code == 429 and err.retry_after == 7.0  # type: ignore[attr-defined]
    for status in (500, 502, 503):
        assert map_error(SpotifyException(status, -1, "x"), "spotify").retryable
    assert isinstance(map_error(SpotifyException(401, -1, "x"), "spotify"), ProviderUnavailable)
    assert isinstance(map_error(SpotifyException(403, -1, "x"), "spotify"), ProviderUnavailable)
    nf = map_error(SpotifyException(404, -1, "x"), "spotify")
    assert not nf.retryable and nf.status_code == 404 and "pkce" in nf.hint


def test_5xx_exhausts_then_raises_retryable(sp: SpotifySource, cassette):
    sp.max_attempts = 2
    with cassette([
        TOKEN,
        interaction("GET", f"{API}/tracks/{TRACK_ID}?market=IT", 503, {"error": {"status": 503}}),
        interaction("GET", f"{API}/tracks/{TRACK_ID}?market=IT", 503, {"error": {"status": 503}}),
    ]):
        with pytest.raises(ProviderError) as exc:
            sp.resolve(f"https://open.spotify.com/track/{TRACK_ID}")
    assert exc.value.retryable and exc.value.status_code == 503


# ------------------------------------------------------------------ ISRC
def test_isrc_absent_by_default_and_fetched_in_batches(creds, opts, cassette):
    album = album_obj(60)
    p1 = [track_obj(n) for n in range(1, 51)]
    p2 = [track_obj(n) for n in range(51, 61)]

    def tracks_resp(ids: list[str]) -> dict:
        return {"tracks": [{**track_obj(int(i[1:].rstrip("x"))), "id": i} for i in ids]}

    ids_all = [_tid(n) for n in range(1, 61)]
    batch1, batch2 = ids_all[:50], ids_all[50:]
    base_interactions = [
        TOKEN,
        interaction("GET", f"{API}/albums/{ALBUM_ID}?market=IT", 200, album),
        interaction("GET", f"{API}/albums/{ALBUM_ID}/tracks/?limit=50&offset=0&market=IT", 200, page(p1, 0, 50, 60)),
        interaction("GET", f"{API}/albums/{ALBUM_ID}/tracks/?limit=50&offset=50&market=IT", 200, page(p2, 50, 50, 60)),
    ]
    url = f"https://open.spotify.com/album/{ALBUM_ID}"

    plain = SpotifySource({**opts, "fetch_isrc": False})
    with cassette(base_interactions):
        tracks = plain.resolve(url)
    assert len(tracks) == 60 and all(t.isrc is None for t in tracks)
    plain.close()

    with_isrc = SpotifySource({**opts, "fetch_isrc": True, "cache_path": str(Path(opts["cache_path"]).with_name("c2.sqlite3"))})
    with cassette(base_interactions + [
        interaction("GET", f"{API}/tracks/?ids={','.join(batch1)}&market=IT", 200, tracks_resp(batch1)),
        interaction("GET", f"{API}/tracks/?ids={','.join(batch2)}&market=IT", 200, tracks_resp(batch2)),
    ]):
        tracks = with_isrc.resolve(url)
    assert len(tracks) == 60
    assert tracks[0].isrc == "GBUM70000001" and tracks[59].isrc == "GBUM70000060"
    with_isrc.close()


# ----------------------------------------------------------------- cache
def test_cache_hit_and_ttl(sp: SpotifySource, cassette, monkeypatch):
    url = f"https://open.spotify.com/track/{TRACK_ID}"
    with cassette([TOKEN, interaction("GET", f"{API}/tracks/{TRACK_ID}?market=IT", 200, track_obj(1, album=album_obj(1)))]):
        first = sp.resolve(url)
    # seconda risoluzione: nessuna cassetta attiva, la rete è bloccata -> deve venire dalla cache
    second = sp.resolve(url)
    assert [t.provider_id for t in second] == [t.provider_id for t in first] and second[0].album == "Discovery"
    # scaduta: la cache viene ignorata e serve di nuovo l'API
    real_time = time.time
    monkeypatch.setattr(spotify_mod.time, "time", lambda: real_time() + 8 * 86400)
    with pytest.raises(Exception):
        sp.resolve(url)
    assert sp._cache.get(canonical_url("track", TRACK_ID)) is None


# ------------------------------------------------------------- download
def test_download_delegates_and_logs_unmatched(sp: SpotifySource, fake_sources, cassette, tmp_path: Path):
    with cassette([TOKEN, interaction("GET", f"{API}/tracks/{TRACK_ID}?market=IT", 200, track_obj(1, album=album_obj(1)))]):
        track = sp.resolve(f"https://open.spotify.com/track/{TRACK_ID}")[0]

    out = sp.download(track, tmp_path / "ok", "best")
    assert out.suffix == ".ogg" and track.extra["match_threshold"] == 0.72
    assert registry.REGISTRY["fakeok"] is sp.delegate()

    track.extra["raise_nomatch"] = True
    with pytest.raises(NoMatch):
        sp.download(track, tmp_path / "nope", "best")
    log_text = (tmp_path / "unmatched.log").read_text(encoding="utf-8")
    assert log_text.count("\n") == 1
    ts, url, query, score, provider = log_text.strip().split("\t")
    # l'URL registrato è quello della traccia restituita dall'API (id del mock), non quello richiesto
    assert url == track.extra["url"] == canonical_url("track", _tid(1))
    assert query == "q" and score == "0.100" and provider == "fakeok"
    assert ts.endswith("Z")


def test_pipeline_end_to_end_with_spotify(creds, opts, fake_sources, cassette, tmp_path: Path):
    registry.configure({"spotify": opts}, ["fakeok"])
    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["spotify", "fakeok"])
    with cassette([
        TOKEN,
        interaction("GET", f"{API}/albums/{ALBUM_ID}?market=IT", 200, album_obj(2)),
        interaction("GET", f"{API}/albums/{ALBUM_ID}/tracks/?limit=50&offset=0&market=IT", 200,
                    page([track_obj(1), track_obj(2)], 0, 50, 2)),
    ]):
        p = Pipeline(cfg)
        summary = p.run([f"https://open.spotify.com/album/{ALBUM_ID}"])
        p.close()
    assert summary.ok and len(summary.done) == 2
    assert summary.done[0].audio_provider == "fakeok" and summary.done[0].source == "spotify"
    assert (tmp_path / "out" / "Daft Punk" / "Discovery" / "01 - Song 1.ogg").exists()


def test_no_deprecated_endpoints():
    text = Path(spotify_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("audio_features", "audio-features", "audio_analysis", "audio-analysis",
                      "recommendations", "related_artists", "related-artists"):
        assert forbidden not in text
