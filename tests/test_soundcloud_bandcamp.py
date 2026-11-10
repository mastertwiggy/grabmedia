import html
import json
import urllib.error
from pathlib import Path

import pytest
import yt_dlp

from core import registry
from sources import _ytdlp, bandcamp as bc_mod, soundcloud as sc_mod
from sources.bandcamp import BandcampSource, classify as bc_classify, extract_data_attrs, tracks_from_page
from sources.base import NoMatch, ProviderError, ProviderUnavailable, Track
from sources.soundcloud import SoundcloudSource, classify as sc_classify


@pytest.fixture
def ffmpeg_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ffmpeg"
    d.mkdir()
    (d / "ffmpeg.exe").write_bytes(b"")
    (d / "ffmpeg").write_bytes(b"")
    return d


@pytest.fixture
def opts(ffmpeg_dir: Path) -> dict:
    return {"ffmpeg_location": str(ffmpeg_dir)}


@pytest.fixture
def fake_extract(monkeypatch):
    calls: list[tuple[str, bool, dict]] = []
    handlers: dict[str, object] = {}

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        calls.append((target, download, dict(self.params)))
        matches = [p for p in handlers if target.startswith(p)]
        if not matches:
            raise yt_dlp.utils.DownloadError(f"ERROR: nessun handler per {target}")
        result = handlers[max(matches, key=len)]
        info = result(target) if callable(result) else result
        if download:
            out_dir = Path(self.params["outtmpl"]["default"]).parent
            ext = _ytdlp.CODEC_EXT[self.params["postprocessors"][0]["preferredcodec"]] or "m4a"
            (out_dir / f"{info['id']}.{ext}").write_bytes(b"audio")
        return info

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    return {"calls": calls, "handlers": handlers}


# ================================================================ SoundCloud
@pytest.fixture
def sc(opts: dict) -> SoundcloudSource:
    src = SoundcloudSource(opts)
    yield src
    src.close()


@pytest.mark.parametrize("url, kind", [
    ("https://soundcloud.com/forss/flickermood", "track"),
    ("https://www.soundcloud.com/forss/flickermood?in=forss/sets/x", "track"),
    ("https://m.soundcloud.com/forss/flickermood", "track"),
    ("https://soundcloud.com/forss/sets/soulhack", "set"),
    ("https://soundcloud.com/forss", "user"),
    ("https://soundcloud.com/forss/tracks", "user"),
    ("https://soundcloud.com/forss/likes", "user"),
    ("https://soundcloud.com/forss/sets", "user"),
    ("https://on.soundcloud.com/AbCdEf", "short"),
    ("https://soundcloud.com/discover", None),
    ("https://soundcloud.com/search?q=x", None),
    ("https://soundcloud.com/", None),
    ("https://api.soundcloud.com/tracks/1", None),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
])
def test_soundcloud_classify_and_can_handle(sc: SoundcloudSource, url: str, kind):
    assert sc_classify(url) == kind
    assert sc.can_handle(url) is (kind is not None)


def test_soundcloud_requires_tools(monkeypatch, opts):
    import shutil

    monkeypatch.delenv("FFMPEG_LOCATION", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ProviderUnavailable, match="ffmpeg"):
        SoundcloudSource({})
    monkeypatch.setattr(_ytdlp, "yt_dlp", None)
    with pytest.raises(ProviderUnavailable, match="yt-dlp"):
        SoundcloudSource(opts)
    with pytest.raises(ProviderUnavailable, match="yt-dlp"):
        BandcampSource(opts)


def test_soundcloud_resolve_track(sc: SoundcloudSource, fake_extract):
    fake_extract["handlers"]["https://soundcloud.com/forss/flickermood"] = {
        "id": "2144", "title": "Flickermood", "uploader": "Forss", "duration": 254.3,
        "webpage_url": "https://soundcloud.com/forss/flickermood", "thumbnail": "https://i1.sndcdn.com/artworks-x-t500x500.jpg",
        "upload_date": "20080417", "timestamp": 1208390000,
    }
    t = sc.resolve("https://soundcloud.com/forss/flickermood")[0]
    assert t.provider == "soundcloud" and t.provider_id == "2144" and t.artists == ["Forss"]
    assert t.cover_url.startswith("https://i1.sndcdn.com/") and t.year == 2008 and t.duration_ms == 254300
    assert t.album is None and t.track_no is None
    assert fake_extract["calls"][-1][2]["noplaylist"] is True


def test_soundcloud_resolve_set_numbers_tracks(sc: SoundcloudSource, fake_extract):
    fake_extract["handlers"]["https://soundcloud.com/forss/sets/soulhack"] = {
        "_type": "playlist", "id": "77", "title": "Soulhack", "webpage_url": "https://soundcloud.com/forss/sets/soulhack",
        "entries": [
            {"id": "1", "title": "One", "uploader": "Forss", "url": "https://soundcloud.com/forss/one", "duration": 100},
            None,
            {"id": "2", "title": "Two", "uploader": "Forss", "url": "https://soundcloud.com/forss/two"},
            {"id": "3", "title": "Three", "uploader": "Forss"},
        ],
    }
    tracks = sc.resolve("https://soundcloud.com/forss/sets/soulhack")
    assert [t.track_no for t in tracks] == [1, 2, 3]
    assert [t.extra["playlist_index"] for t in tracks] == [1, 2, 3]
    assert all(t.extra["playlist"] == "Soulhack" and t.album is None for t in tracks)
    assert tracks[2].extra["url"] == "https://api.soundcloud.com/tracks/3"
    # i set si estraggono completi (in extract_flat yt-dlp dà solo URL senza titolo)
    params = fake_extract["calls"][-1][2]
    assert "extract_flat" not in params and params["noplaylist"] is False


def test_soundcloud_short_link_to_untitled_set_is_reextracted(sc: SoundcloudSource, fake_extract, monkeypatch):
    monkeypatch.setattr(sc_mod, "expand_short_url", lambda url, options=None, **kw: None)
    flat = {"_type": "playlist", "id": "77", "title": "Soulhack", "webpage_url": "https://soundcloud.com/forss/sets/soulhack",
            "entries": [{"_type": "url", "id": str(i), "url": f"https://api.soundcloud.com/tracks/{i}"} for i in range(1, 4)]}
    full = {**flat, "entries": [{"id": str(i), "title": f"T{i}", "uploader": "Forss"} for i in range(1, 4)]}

    def handler(target):
        params = fake_extract["calls"][-1][2]  # la chiamata corrente è già registrata
        return flat if params.get("extract_flat") else full

    fake_extract["handlers"]["https://on.soundcloud.com/xyz"] = handler
    tracks = sc.resolve("https://on.soundcloud.com/xyz")
    assert [t.title for t in tracks] == ["T1", "T2", "T3"] and [t.track_no for t in tracks] == [1, 2, 3]
    modes = ["flat" if c[2].get("extract_flat") else "full" for c in fake_extract["calls"]]
    assert modes[0] == "flat" and modes[-1] == "full"


def test_soundcloud_resolve_profile_lists_tracks_without_numbers(sc: SoundcloudSource, fake_extract):
    fake_extract["handlers"]["https://soundcloud.com/forss"] = {
        "_type": "playlist", "id": "forss", "title": "Forss (All)",
        "entries": [{"id": str(i), "title": f"T{i}", "uploader": "Forss", "url": f"https://soundcloud.com/forss/t{i}"} for i in range(1, 4)],
    }
    tracks = sc.resolve("https://soundcloud.com/forss")
    assert [t.provider_id for t in tracks] == ["1", "2", "3"]
    assert all(t.track_no is None and t.extra["playlist_index"] for t in tracks)


def test_soundcloud_short_link_single_track(sc: SoundcloudSource, fake_extract, monkeypatch):
    # redirect non seguibile (rete assente): si ricade sull'estrazione piatta dell'URL breve
    monkeypatch.setattr(sc_mod, "expand_short_url", lambda url, options=None, **kw: None)
    fake_extract["handlers"]["https://on.soundcloud.com/abc"] = {
        "id": "9", "title": "Short", "uploader": "U", "webpage_url": "https://soundcloud.com/u/short",
    }
    tracks = sc.resolve("https://on.soundcloud.com/abc")
    assert len(tracks) == 1 and tracks[0].provider_id == "9"


def test_soundcloud_short_link_is_classified_after_redirect(sc: SoundcloudSource, fake_extract, monkeypatch):
    # un link breve verso un profilo deve essere trattato come profilo, non come brano
    monkeypatch.setattr(sc_mod, "expand_short_url", lambda url, options=None, **kw: "https://soundcloud.com/forss")
    fake_extract["handlers"]["https://soundcloud.com/forss"] = {
        "_type": "playlist", "id": "forss", "title": "Forss",
        "entries": [{"id": "1", "title": "A", "uploader": "Forss", "url": "https://soundcloud.com/forss/a"},
                    {"id": "2", "title": "B", "uploader": "Forss", "url": "https://soundcloud.com/forss/b"}],
    }
    tracks = sc.resolve("https://on.soundcloud.com/abc")
    assert [t.title for t in tracks] == ["A", "B"] and all(t.track_no is None for t in tracks)
    assert fake_extract["calls"][-1][0] == "https://soundcloud.com/forss"
    assert fake_extract["calls"][-1][2].get("extract_flat") == "in_playlist"


def test_soundcloud_delegated_search_match(sc: SoundcloudSource, fake_extract, tmp_path: Path):
    fake_extract["handlers"]["scsearch10:"] = {"entries": [
        {"id": "11", "title": "Flickermood (Live)", "uploader": "Forss", "duration": 254, "webpage_url": "https://soundcloud.com/forss/live"},
        {"id": "12", "title": "Flickermood", "uploader": "Forss", "duration": 254, "webpage_url": "https://soundcloud.com/forss/flickermood"},
    ]}
    fake_extract["handlers"]["https://soundcloud.com/forss/flickermood"] = {"id": "12", "title": "Flickermood"}
    track = Track("fakemeta", "m", "Flickermood", ["Forss"], duration_ms=254000)
    final = sc.download(track, tmp_path / "f", "mp3")
    assert final.suffix == ".mp3" and track.extra["matched_track_id"] == "12" and track.extra["match_score"] >= 0.9
    search = next(c for c in fake_extract["calls"] if c[0].startswith("scsearch"))
    assert search[0] == "scsearch10:Forss Flickermood" and search[1] is False
    assert fake_extract["calls"][-1][0] == "https://soundcloud.com/forss/flickermood"


def test_soundcloud_delegated_search_below_threshold(sc: SoundcloudSource, fake_extract, tmp_path: Path):
    fake_extract["handlers"]["scsearch10:"] = {"entries": [
        {"id": "1", "title": "Something else entirely", "uploader": "Nobody", "duration": 30},
    ]}
    track = Track("fakemeta", "m", "Flickermood", ["Forss"], duration_ms=254000)
    with pytest.raises(NoMatch) as exc:
        sc.download(track, tmp_path / "f", "best")
    assert exc.value.query == "Forss Flickermood" and "Forss+Flickermood" in exc.value.hint
    assert exc.value.retryable is False and not any(c[1] for c in fake_extract["calls"])


def test_soundcloud_own_track_downloads_by_url(sc: SoundcloudSource, fake_extract, tmp_path: Path):
    fake_extract["handlers"]["https://api.soundcloud.com/tracks/5"] = {"id": "5", "title": "x"}
    final = sc.download(Track("soundcloud", "5", "x"), tmp_path / "x", "opus")
    assert final.suffix == ".opus" and fake_extract["calls"][-1][2]["ffmpeg_location"] == sc.options["ffmpeg_location"]


def test_soundcloud_429_is_retryable(sc: SoundcloudSource, fake_extract):
    def boom(target):
        raise yt_dlp.utils.DownloadError("ERROR: [soundcloud] 1: Unable to download JSON metadata: HTTP Error 429: Too Many Requests")

    fake_extract["handlers"]["https://soundcloud.com/forss/flickermood"] = boom
    with pytest.raises(ProviderError) as exc:
        sc.resolve("https://soundcloud.com/forss/flickermood")
    assert exc.value.retryable and exc.value.status_code == 429


# ================================================================== Bandcamp
TRALBUM = {
    "artist": "Nils Frahm",
    "art_id": 1234567890,
    "item_type": "album",
    "album_release_date": "15 Sep 2015 00:00:00 GMT",
    "current": {"id": 999, "title": "Solo", "type": "album", "release_date": "15 Sep 2015 00:00:00 GMT"},
    "trackinfo": [
        {"id": 1, "title": "Ode", "track_num": 1, "duration": 300.5, "title_link": "/track/ode",
         "file": {"mp3-128": "https://t4.bcbits.com/stream/abc/mp3-128/1?p=0&ts=1"}, "is_downloadable": True, "artist": None},
        {"id": 2, "title": "Some", "track_num": 2, "duration": 200.0, "title_link": "/track/some",
         "file": None, "is_downloadable": False, "artist": "Nils Frahm & Friend"},
        {"id": 3, "title": "Wall", "track_num": 3, "duration": 100.25, "title_link": "/track/wall",
         "file": {"mp3-128": "https://t4.bcbits.com/stream/def/mp3-128/3?p=0&ts=1"}, "is_downloadable": False},
        {"id": 4, "title": "", "track_num": 4},  # senza titolo: scartata
    ],
}


def page_html(tralbum: dict, embed: dict | None = None) -> str:
    parts = [f'<script data-tralbum="{html.escape(json.dumps(tralbum), quote=True)}" data-cart="x"></script>']
    if embed is not None:
        parts.append(f'<script data-embed="{html.escape(json.dumps(embed), quote=True)}"></script>')
    return "<html><body>" + "".join(parts) + "</body></html>"


@pytest.fixture
def bc(opts: dict) -> BandcampSource:
    src = BandcampSource(opts)
    yield src
    src.close()


@pytest.mark.parametrize("url, kind", [
    ("https://nilsfrahm.bandcamp.com/album/solo", "album"),
    ("https://nilsfrahm.bandcamp.com/track/ode", "track"),
    ("http://nilsfrahm.bandcamp.com/track/ode/", "track"),
    ("https://nilsfrahm.bandcamp.com/", None),
    ("https://nilsfrahm.bandcamp.com/music", None),
    ("https://bandcamp.com/album/solo", None),
    ("https://daily.bandcamp.com/album/solo", None),
    ("https://soundcloud.com/forss/flickermood", None),
])
def test_bandcamp_classify_and_can_handle(bc: BandcampSource, url: str, kind):
    assert bc_classify(url) == kind
    assert bc.can_handle(url) is (kind is not None)


def test_bandcamp_extract_data_attrs_unescapes():
    attrs = extract_data_attrs(page_html({"a": 1, "s": "x&y \"q\""}, {"album_title": "Z"}))
    assert attrs["tralbum"] == {"a": 1, "s": 'x&y "q"'} and attrs["embed"] == {"album_title": "Z"}
    assert extract_data_attrs("<html></html>") == {}


def test_bandcamp_tracks_from_album_page():
    url = "https://nilsfrahm.bandcamp.com/album/solo"
    tracks = tracks_from_page(page_html(TRALBUM), url)
    assert [t.title for t in tracks] == ["Ode", "Some", "Wall"]
    first = tracks[0]
    assert first.provider == "bandcamp" and first.provider_id == "1"
    assert first.artists == ["Nils Frahm"] and tracks[1].artists == ["Nils Frahm & Friend"]
    assert all(t.album == "Solo" and t.year == 2015 for t in tracks)
    assert [t.track_no for t in tracks] == [1, 2, 3]
    assert first.duration_ms == 300500 and tracks[2].duration_ms == 100250
    assert all(t.cover_url == "https://f4.bcbits.com/img/a1234567890_10.jpg" for t in tracks)
    assert first.extra["url"] == "https://nilsfrahm.bandcamp.com/track/ode" and first.extra["art_id"] == 1234567890
    assert first.extra["downloadable"] is True and first.extra["stream_url"].startswith("https://t4.bcbits.com/stream/abc")
    assert "stream_url" not in tracks[1].extra and tracks[1].extra["downloadable"] is False
    assert first.extra["albumartist"] == "Nils Frahm"


def test_bandcamp_track_page_uses_embed_album_title():
    tralbum = {**TRALBUM, "item_type": "track", "current": {"id": 1, "title": "Ode", "type": "track"},
               "trackinfo": [TRALBUM["trackinfo"][0]]}
    tracks = tracks_from_page(page_html(tralbum, {"album_title": "Solo (embed)"}), "https://nilsfrahm.bandcamp.com/track/ode")
    assert len(tracks) == 1 and tracks[0].album == "Solo (embed)" and tracks[0].year == 2015
    tracks = tracks_from_page(page_html(tralbum), "https://nilsfrahm.bandcamp.com/track/ode")
    assert tracks[0].album is None  # nessun campo inventato


def test_bandcamp_resolve_uses_http_mock(bc: BandcampSource, monkeypatch, fake_extract):
    seen = {}

    def fake_fetch(url, options=None, **kw):
        seen["url"], seen["options"] = url, options
        return page_html(TRALBUM)

    monkeypatch.setattr(bc_mod, "fetch_text", fake_fetch)
    tracks = bc.resolve("https://nilsfrahm.bandcamp.com/album/solo")
    assert len(tracks) == 3 and seen["url"] == "https://nilsfrahm.bandcamp.com/album/solo" and seen["options"] is bc.options

    # senza data-tralbum (es. pagina "Client Challenge") si passa a yt-dlp; se anche lui fallisce, errore azionabile
    monkeypatch.setattr(bc_mod, "fetch_text", lambda url, options=None, **kw: "<html><title>Client Challenge</title></html>")
    with pytest.raises(ProviderError) as exc:
        bc.resolve("https://nilsfrahm.bandcamp.com/album/solo")
    assert "cookiefile" in (exc.value.hint or "") or "yt-dlp" in str(exc.value)

    def forbidden(url, options=None, **kw):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    bc.challenged = False  # il passo precedente ha attivato il flag di sessione
    monkeypatch.setattr(bc_mod, "fetch_text", forbidden)
    with pytest.raises(ProviderError) as exc:
        bc.resolve("https://nilsfrahm.bandcamp.com/album/solo")
    assert exc.value.status_code == 403 and "cookiefile" in exc.value.hint and not exc.value.retryable


def test_bandcamp_resolve_falls_back_to_ytdlp_on_challenge_page(bc: BandcampSource, monkeypatch, fake_extract):
    monkeypatch.setattr(bc_mod, "fetch_text", lambda url, options=None, **kw: "<html><title>Client Challenge</title></html>")
    fake_extract["handlers"]["https://nilsfrahm.bandcamp.com/album/solo"] = {
        "_type": "playlist", "id": "solo", "title": "Solo", "uploader": "Nils Frahm",
        "entries": [
            {"id": "1", "title": "Ode", "uploader": "Nils Frahm", "album": "Solo", "track_number": 1, "duration": 300,
             "release_date": "20150915", "thumbnail": "https://f4.bcbits.com/img/a1234567890_10.jpg",
             "webpage_url": "https://nilsfrahm.bandcamp.com/track/ode"},
            {"id": "2", "title": "Some", "uploader": "Nils Frahm", "webpage_url": "https://nilsfrahm.bandcamp.com/track/some"},
        ],
    }
    tracks = bc.resolve("https://nilsfrahm.bandcamp.com/album/solo")
    assert [t.title for t in tracks] == ["Ode", "Some"] and [t.track_no for t in tracks] == [1, 2]
    assert tracks[0].album == "Solo" and tracks[0].year == 2015 and tracks[0].cover_url.endswith("a1234567890_10.jpg")
    assert tracks[1].album == "Solo" and tracks[1].extra["downloadable"] is False
    assert tracks[1].extra["url"] == "https://nilsfrahm.bandcamp.com/track/some"
    params = fake_extract["calls"][-1][2]
    assert "extract_flat" not in params and params["noplaylist"] is False


def test_bandcamp_download_direct_when_downloadable(bc: BandcampSource, monkeypatch, fake_extract, tmp_path: Path):
    fetched = {}

    def fake_fetch_to_file(url, dest, options=None, *, progress_cb=None, **kw):
        fetched["url"] = url
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ID3mp3data")
        if progress_cb:
            progress_cb(50, "download")
        return dest

    monkeypatch.setattr(bc_mod, "fetch_to_file", fake_fetch_to_file)
    track = tracks_from_page(page_html(TRALBUM), "https://nilsfrahm.bandcamp.com/album/solo")[0]
    progress: list[float] = []
    final = bc.download(track, tmp_path / "Nils Frahm" / "Solo" / "01 - Ode", "best", lambda p, m: progress.append(p))
    assert final == tmp_path / "Nils Frahm" / "Solo" / "01 - Ode.mp3" and final.read_bytes() == b"ID3mp3data"
    assert fetched["url"].startswith("https://t4.bcbits.com/stream/abc") and progress[-1] == 100.0
    assert not fake_extract["calls"]  # yt-dlp non usato
    # con un codec diverso da mp3 si passa a yt-dlp anche se scaricabile
    fake_extract["handlers"]["https://nilsfrahm.bandcamp.com/track/ode"] = {"id": "1", "title": "Ode"}
    final = bc.download(track, tmp_path / "ode-opus", "opus")
    assert final.suffix == ".opus" and fake_extract["calls"][-1][0] == "https://nilsfrahm.bandcamp.com/track/ode"


def test_bandcamp_download_falls_back_to_ytdlp_without_file(bc: BandcampSource, monkeypatch, fake_extract, tmp_path: Path):
    monkeypatch.setattr(bc_mod, "fetch_to_file", lambda *a, **k: pytest.fail("non deve scaricare direttamente"))
    tracks = tracks_from_page(page_html(TRALBUM), "https://nilsfrahm.bandcamp.com/album/solo")
    fake_extract["handlers"]["https://nilsfrahm.bandcamp.com/track/some"] = {"id": "2", "title": "Some"}
    fake_extract["handlers"]["https://nilsfrahm.bandcamp.com/track/wall"] = {"id": "3", "title": "Wall"}
    final = bc.download(tracks[1], tmp_path / "some", "mp3_320")  # nessun file
    assert final.suffix == ".mp3" and fake_extract["calls"][-1][0] == "https://nilsfrahm.bandcamp.com/track/some"
    pp = fake_extract["calls"][-1][2]["postprocessors"][0]
    assert pp["preferredcodec"] == "mp3" and pp["preferredquality"] == "320"
    final = bc.download(tracks[2], tmp_path / "wall", "best")  # file presente ma non scaricabile
    assert final.suffix == ".m4a" and fake_extract["calls"][-1][0] == "https://nilsfrahm.bandcamp.com/track/wall"


def test_bandcamp_direct_http_error_falls_back_or_retries(bc: BandcampSource, monkeypatch, fake_extract, tmp_path: Path):
    track = tracks_from_page(page_html(TRALBUM), "https://nilsfrahm.bandcamp.com/album/solo")[0]

    def gone(url, dest, options=None, **kw):
        raise urllib.error.HTTPError(url, 410, "Gone", {}, None)

    monkeypatch.setattr(bc_mod, "fetch_to_file", gone)
    fake_extract["handlers"]["https://nilsfrahm.bandcamp.com/track/ode"] = {"id": "1", "title": "Ode"}
    final = bc.download(track, tmp_path / "ode", "mp3")
    assert final.suffix == ".mp3" and fake_extract["calls"][-1][0] == "https://nilsfrahm.bandcamp.com/track/ode"

    def busy(url, dest, options=None, **kw):
        raise urllib.error.HTTPError(url, 503, "Busy", {}, None)

    monkeypatch.setattr(bc_mod, "fetch_to_file", busy)
    with pytest.raises(ProviderError) as exc:
        bc.download(track, tmp_path / "ode2", "mp3")
    assert exc.value.retryable and exc.value.status_code == 503


def test_bandcamp_refuses_delegation_and_registry_excludes_it(bc: BandcampSource, opts: dict, tmp_path: Path):
    with pytest.raises(ProviderError, match="non ha una ricerca"):
        bc.download(Track("fakemeta", "m", "t", ["a"]), tmp_path / "t", "best")
    registry.configure({"bandcamp": opts, "soundcloud": opts, "youtube": opts, "generic": opts})
    from sources.base import MetadataSource

    class Meta(MetadataSource):
        name = "meta"

        def can_handle(self, url):
            return False

        def resolve(self, url):
            return []

    meta = Meta()
    meta.audio_provider = "bandcamp"
    with pytest.raises(ProviderError, match="non accetta deleghe"):
        registry.audio_provider_for(meta)
    meta.audio_provider = "soundcloud"
    assert registry.audio_provider_for(meta).name == "soundcloud"


def test_bandcamp_cookiefile_invalid_is_actionable(tmp_path: Path, opts: dict, monkeypatch):
    bad = tmp_path / "cookies.txt"
    bad.write_text("not a netscape file", encoding="utf-8")
    src = BandcampSource({**opts, "cookiefile": str(bad)})
    with pytest.raises(ProviderError, match="cookiefile"):
        bc_mod.fetch_text("https://nilsfrahm.bandcamp.com/album/solo", src.options)
