import shutil
from pathlib import Path

import pytest
import yt_dlp

from core import registry, state
from core.config import Config
from core.pipeline import Pipeline
from sources import _ytdlp
from sources.base import MetadataSource, NoMatch, ProviderError, ProviderUnavailable, Track
from sources.generic import GenericSource
from sources.youtube import YoutubeSource, classify

VIDEO_INFO = {
    "id": "dQw4w9WgXcQ", "title": "Rick Astley - Never Gonna Give You Up (Official Video)",
    "uploader": "Rick Astley", "channel": "Rick Astley", "duration": 213.0,
    "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hq.jpg", "upload_date": "20091025",
    "track": "Never Gonna Give You Up", "artist": "Rick Astley", "album": "Whenever You Need Somebody",
    "release_year": 1987,
}


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
def yt(opts: dict) -> YoutubeSource:
    src = YoutubeSource(opts)
    yield src
    src.close()


@pytest.fixture
def fake_extract(monkeypatch):
    """Sostituisce YoutubeDL.extract_info: ``handlers[prefix] -> info`` e per
    ``download=True`` scrive un finto file audio nella cartella di outtmpl."""
    calls: list[tuple[str, bool, dict]] = []
    handlers: dict[str, object] = {}

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        calls.append((target, download, dict(self.params)))
        matches = [p for p in handlers if target.startswith(p)]
        if not matches:
            raise yt_dlp.utils.DownloadError(f"ERROR: nessun handler per {target}")
        result = handlers[max(matches, key=len)]  # prefisso più specifico
        info = result(target) if callable(result) else result
        if download:
            out_dir = Path(self.params["outtmpl"]["default"]).parent
            codec = self.params["postprocessors"][0]["preferredcodec"]
            ext = _ytdlp.CODEC_EXT[codec] or "m4a"  # "best" = formato nativo: nel mock è m4a
            (out_dir / f"{info['id']}.{ext}").write_bytes(b"audio")
        return info

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    return {"calls": calls, "handlers": handlers}


# ---------------------------------------------------------------- can_handle
@pytest.mark.parametrize("url, kind", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "video"),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ&list=PL123", "video"),
    ("https://youtu.be/dQw4w9WgXcQ", "video"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "video"),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "video"),
    ("https://www.youtube.com/playlist?list=PL123", "playlist"),
    ("https://music.youtube.com/playlist?list=OLAK5uy_abc", "playlist"),
    ("https://music.youtube.com/browse/MPREb_abc", "playlist"),
    ("https://www.youtube.com/@RickAstleyYT", "channel"),
    ("https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw", "channel"),
    ("https://www.youtube.com/c/RickAstley", "channel"),
    ("https://www.youtube.com/user/RickAstley", "channel"),
    ("https://www.youtube.com/", None),
    ("https://soundcloud.com/artist/track", None),
    ("https://vimeo.com/123", None),
    ("ftp://youtube.com/watch?v=dQw4w9WgXcQ", None),
    ("not a url", None),
])
def test_classify_and_can_handle(yt: YoutubeSource, url: str, kind):
    assert classify(url) == kind
    assert yt.can_handle(url) is (kind is not None)


def test_generic_can_handle_only_unclaimed_http(opts: dict, monkeypatch):
    registry.configure({"youtube": opts, "generic": opts})
    generic = registry.load("generic")
    assert generic is not None and isinstance(generic, GenericSource)
    assert generic.can_handle("https://soundcloud.com/a/b")
    assert generic.can_handle("http://example.com/audio.mp3")
    assert not generic.can_handle("ftp://example.com/x")
    assert not generic.can_handle("file:///tmp/x.mp3")
    assert not generic.can_handle("example.com/x")
    # finché youtube non è caricato, generic rivendica anche YouTube...
    assert generic.can_handle("https://youtu.be/dQw4w9WgXcQ")
    registry.load("youtube")
    # ...dopo no: un altro provider la rivendica
    assert not generic.can_handle("https://youtu.be/dQw4w9WgXcQ")
    assert registry.dispatch("https://youtu.be/dQw4w9WgXcQ", order=["generic", "youtube"]).name == "youtube"
    # un host che nessun provider (nemmeno uno non disponibile) rivendica finisce al passthrough
    assert registry.dispatch("https://example.com/audio/x", order=["youtube", "generic"]).name == "generic"


# ----------------------------------------------------------------- init
def test_init_requires_ffmpeg(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("FFMPEG_LOCATION", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ProviderUnavailable, match="ffmpeg"):
        YoutubeSource({})
    with pytest.raises(ProviderUnavailable, match="ffmpeg non trovato"):
        YoutubeSource({"ffmpeg_location": str(tmp_path / "nope")})
    with pytest.raises(ProviderUnavailable):
        GenericSource({})
    monkeypatch.setattr(shutil, "which", lambda name: "C:/ffmpeg/ffmpeg.exe")
    assert YoutubeSource({}).ffmpeg is None  # nel PATH: nessuna location esplicita


def test_init_requires_ytdlp(monkeypatch, opts: dict):
    monkeypatch.setattr(_ytdlp, "yt_dlp", None)
    with pytest.raises(ProviderUnavailable, match="yt-dlp"):
        YoutubeSource(opts)
    with pytest.raises(ProviderUnavailable, match="yt-dlp"):
        GenericSource(opts)


def test_registry_reports_unavailable_not_crash(monkeypatch):
    monkeypatch.delenv("FFMPEG_LOCATION", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert registry.load("youtube") is None
    assert "ffmpeg" in registry.UNAVAILABLE["youtube"]
    assert registry.load("generic") is None


# -------------------------------------------------------------- resolve
def test_resolve_single_video_not_expanded(yt: YoutubeSource, fake_extract):
    fake_extract["handlers"]["https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1"] = VIDEO_INFO
    tracks = yt.resolve("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1")
    assert len(tracks) == 1
    t = tracks[0]
    assert t.provider == "youtube" and t.provider_id == "dQw4w9WgXcQ"
    assert t.title == "Never Gonna Give You Up" and t.artists == ["Rick Astley"]
    assert t.album == "Whenever You Need Somebody" and t.year == 1987
    assert t.duration_ms == 213000 and t.cover_url.startswith("https://i.ytimg.com")
    assert t.extra["url"] == VIDEO_INFO["webpage_url"] and t.extra["upload_year"] == 2009
    _, download, params = fake_extract["calls"][-1]
    assert download is False and params["noplaylist"] is True and "extract_flat" not in params


def test_resolve_video_without_music_metadata_invents_nothing(yt: YoutubeSource, fake_extract):
    info = {"id": "abc12345678", "title": "Some upload", "uploader": "Channel - Topic", "duration": 10}
    fake_extract["handlers"]["https://youtu.be/abc12345678"] = info
    t = yt.resolve("https://youtu.be/abc12345678")[0]
    assert t.title == "Some upload" and t.artists == ["Channel"]
    assert t.album is None and t.year is None and t.track_no is None and t.cover_url is None


def test_resolve_playlist_flat(yt: YoutubeSource, fake_extract):
    entries = [
        {"id": "aaaaaaaaaaa", "title": "First", "uploader": "U", "duration": 100, "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
        None,
        {"id": "bbbbbbbbbbb", "title": "Second", "uploader": "U", "duration": 200},
    ]
    fake_extract["handlers"]["https://www.youtube.com/playlist?list=PL1"] = {
        "_type": "playlist", "id": "PL1", "title": "My Mix", "entries": entries,
    }
    tracks = yt.resolve("https://www.youtube.com/playlist?list=PL1")
    assert [t.provider_id for t in tracks] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert tracks[0].album is None and tracks[0].track_no is None  # playlist generica: niente album inventato
    assert tracks[0].extra["playlist"] == "My Mix" and tracks[1].extra["playlist_index"] == 2
    assert tracks[1].extra["url"] == "https://www.youtube.com/watch?v=bbbbbbbbbbb"
    _, _, params = fake_extract["calls"][-1]
    assert params["extract_flat"] == "in_playlist" and params["noplaylist"] is False


def test_resolve_album_playlist_sets_album_and_track_no(yt: YoutubeSource, fake_extract):
    fake_extract["handlers"]["https://music.youtube.com/playlist?list=OLAK5uy_x"] = {
        "_type": "playlist", "id": "OLAK5uy_x", "title": "Discovery",
        "entries": [{"id": f"{i}" * 11, "title": f"T{i}", "uploader": "Daft Punk - Topic", "duration": 100} for i in range(1, 4)],
    }
    tracks = yt.resolve("https://music.youtube.com/playlist?list=OLAK5uy_x")
    assert [t.track_no for t in tracks] == [1, 2, 3]
    assert all(t.album == "Discovery" and t.artists == ["Daft Punk"] for t in tracks)


def test_resolve_channel_flattens_nested_tabs(yt: YoutubeSource, fake_extract):
    fake_extract["handlers"]["https://www.youtube.com/@Artist"] = {
        "_type": "playlist", "id": "UC1", "title": "Artist",
        "entries": [
            {"_type": "url", "ie_key": "YoutubeTab", "url": "https://www.youtube.com/@Artist/videos", "id": "UC1-videos"},
            {"id": "ccccccccccc", "title": "Loose video", "duration": 50},
        ],
    }
    fake_extract["handlers"]["https://www.youtube.com/@Artist/videos"] = {
        "_type": "playlist", "id": "UC1-videos", "title": "Artist - Videos",
        "entries": [{"id": "ddddddddddd", "title": "In tab", "duration": 60}],
    }
    ids = [t.provider_id for t in yt.resolve("https://www.youtube.com/@Artist")]
    assert ids == ["ddddddddddd", "ccccccccccc"]


def test_resolve_error_is_mapped(yt: YoutubeSource, fake_extract):
    # messaggio sconosciuto -> "unknown": un solo nuovo tentativo, poi permanente
    with pytest.raises(ProviderError) as exc:
        yt.resolve("https://youtu.be/zzzzzzzzzzz")
    assert exc.value.provider == "youtube" and exc.value.kind == "unknown"
    assert exc.value.retryable and exc.value.retry_limit == 1

    def gone(target):
        raise yt_dlp.utils.DownloadError("ERROR: [youtube] zzzzzzzzzzz: Video unavailable")

    fake_extract["handlers"]["https://youtu.be/zzzzzzzzzzz"] = gone
    with pytest.raises(ProviderError) as exc:
        yt.resolve("https://youtu.be/zzzzzzzzzzz")
    assert exc.value.kind == "missing" and not exc.value.retryable and "rimosso" in exc.value.hint


# ------------------------------------------------------------- download
def _search_entries():
    return [
        {"id": "coverxxxxxx", "title": "Never Gonna Give You Up (Cover)", "uploader": "Someone", "duration": 213},
        {"id": "dQw4w9WgXcQ", "title": "Rick Astley - Never Gonna Give You Up (Official Video)", "uploader": "Rick Astley", "duration": 213},
        {"id": "livexxxxxxx", "title": "Rick Astley - Never Gonna Give You Up (Live)", "uploader": "Rick Astley", "duration": 240},
    ]


def test_download_delegated_track_searches_and_matches(yt: YoutubeSource, fake_extract, tmp_path: Path):
    fake_extract["handlers"]["ytsearch5:"] = {"_type": "playlist", "entries": _search_entries()}
    fake_extract["handlers"]["https://www.youtube.com/watch?v=dQw4w9WgXcQ"] = VIDEO_INFO
    track = Track("fakemeta", "m1", "Never Gonna Give You Up", ["Rick Astley"], duration_ms=213000)
    progress: list[float] = []
    dest = tmp_path / "out" / "Rick Astley" / "01 - Never Gonna Give You Up"
    final = yt.download(track, dest, "best", lambda p, m: progress.append(p))
    assert final == dest.with_name(dest.name + ".m4a") and final.read_bytes() == b"audio"
    assert track.extra["matched_video_id"] == "dQw4w9WgXcQ" and track.extra["match_score"] >= 0.9
    assert progress[-1] == 100.0
    search_call = next(c for c in fake_extract["calls"] if c[0].startswith("ytsearch"))
    assert search_call[0] == "ytsearch5:Rick Astley Never Gonna Give You Up" and search_call[1] is False
    dl_call = fake_extract["calls"][-1]
    assert dl_call[1] is True
    params = dl_call[2]
    assert params["format"] == "bestaudio/best" and params["noplaylist"] is True
    assert params["quiet"] and params["no_warnings"] and params["retries"] == 0
    # "best" = formato nativo, nessuna transcodifica
    assert params["postprocessors"][0] == {"key": "FFmpegExtractAudio", "preferredcodec": "best"}
    assert params["ffmpeg_location"] == yt.options["ffmpeg_location"]
    assert not list((dest.parent).glob("grabmedia-*"))


def test_download_own_track_uses_provider_id_without_search(yt: YoutubeSource, fake_extract, tmp_path: Path):
    fake_extract["handlers"]["https://www.youtube.com/watch?v=dQw4w9WgXcQ"] = VIDEO_INFO
    track = Track("youtube", "dQw4w9WgXcQ", "Never Gonna Give You Up", ["Rick Astley"])
    final = yt.download(track, tmp_path / "song", "mp3_320")
    assert final.suffix == ".mp3" and "matched_video_id" not in track.extra
    assert not any(c[0].startswith("ytsearch") for c in fake_extract["calls"])
    pp = fake_extract["calls"][-1][2]["postprocessors"][0]
    assert pp == {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}


def test_download_no_match_raises_nomatch_with_query(yt: YoutubeSource, fake_extract, tmp_path: Path):
    fake_extract["handlers"]["ytsearch5:"] = {"entries": [
        {"id": "junkxxxxxxx", "title": "Totally unrelated", "uploader": "Nobody", "duration": 10},
    ]}
    track = Track("fakemeta", "m2", "Never Gonna Give You Up", ["Rick Astley"], duration_ms=213000)
    with pytest.raises(NoMatch) as exc:
        yt.download(track, tmp_path / "x", "best")
    err = exc.value
    assert err.retryable is False and err.query == "Rick Astley Never Gonna Give You Up"
    assert "Rick+Astley+Never+Gonna" in (err.hint or "") and "match_threshold" in err.hint
    assert "matched_video_id" not in track.extra
    assert not (tmp_path / "x.m4a").exists()


def test_download_cookiefile_option_is_passed(opts: dict, fake_extract, tmp_path: Path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    src = YoutubeSource({**opts, "cookiefile": str(cookies)})
    fake_extract["handlers"]["https://www.youtube.com/watch?v=dQw4w9WgXcQ"] = VIDEO_INFO
    src.download(Track("youtube", "dQw4w9WgXcQ", "t"), tmp_path / "t", "opus")
    assert fake_extract["calls"][-1][2]["cookiefile"] == str(cookies)
    src.close()


def test_pipeline_marks_nomatch_as_skipped(fake_sources, tmp_path: Path):
    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok", "fakemeta"])
    meta = registry.load("fakemeta")
    meta.resolve = lambda url: [Track("fakemeta", "nm", "No Match", ["X"], extra={"raise_nomatch": True})]
    summary = Pipeline(cfg).run(["https://fakemeta.test/x"])
    assert summary.ok and not summary.failed
    assert len(summary.skipped) == 1 and summary.skipped[0].reason == "nessun match affidabile"
    db = state.StateDB(cfg.state_db)
    rec = db.get("fakemeta:nm")
    assert rec.status == state.SKIPPED and "nessun match" in rec.error
    db.close()


# ------------------------------------------------------------- generic
def test_generic_is_never_audio_provider(opts: dict, monkeypatch):
    registry.configure({"youtube": opts, "generic": opts})

    class Meta(MetadataSource):
        name = "meta"

        def can_handle(self, url):
            return False

        def resolve(self, url):
            return []

    meta = Meta()
    meta.audio_provider = "generic"
    with pytest.raises(ProviderError, match="non accetta deleghe"):
        registry.audio_provider_for(meta)
    meta.audio_provider = None
    monkeypatch.setattr(registry, "available_modules", lambda: ["generic"])
    with pytest.raises(ProviderUnavailable):
        registry.audio_provider_for(meta)
    monkeypatch.setattr(registry, "available_modules", lambda: ["generic", "youtube"])
    assert registry.audio_provider_for(meta).name == "youtube"


def test_generic_download_refuses_foreign_tracks(opts: dict, tmp_path: Path):
    g = GenericSource(opts)
    with pytest.raises(ProviderError, match="solo URL diretti"):
        g.download(Track("fakemeta", "m", "t", ["a"]), tmp_path / "t", "best")


def test_generic_resolve_and_download(opts: dict, fake_extract, tmp_path: Path):
    g = GenericSource(opts)
    fake_extract["handlers"]["https://example.com/track"] = {
        "id": "ex1", "title": "Example Track", "uploader": "Ex Artist", "duration": 30.5,
        "webpage_url": "https://example.com/track", "extractor_key": "Generic",
    }
    tracks = g.resolve("https://example.com/track")
    assert len(tracks) == 1
    t = tracks[0]
    assert t.provider == "generic" and t.provider_id == "https://example.com/track"
    assert t.artists == ["Ex Artist"] and t.duration_ms == 30500 and t.extra["extractor"] == "Generic"
    final = g.download(t, tmp_path / "ex", "flac")
    assert final.suffix == ".flac" and fake_extract["calls"][-1][0] == "https://example.com/track"

    fake_extract["handlers"]["https://example.com/set"] = {
        "_type": "playlist", "title": "A Set",
        "entries": [
            {"id": "s1", "title": "One", "url": "https://example.com/one", "duration": 10},
            {"id": "s2", "title": "Two", "webpage_url": "https://example.com/two"},
            {"id": "s3", "title": "No url"},
        ],
    }
    tracks = g.resolve("https://example.com/set")
    assert [t.provider_id for t in tracks] == ["https://example.com/one", "https://example.com/two"]
    assert tracks[1].extra["playlist"] == "A Set" and tracks[1].extra["playlist_index"] == 2
    g.close()


# ------------------------------------------------------------- helper
@pytest.mark.parametrize("text, status, retryable", [
    ("ERROR: [youtube] abc: HTTP Error 429: Too Many Requests", 429, True),
    ("ERROR: Unable to download webpage: HTTP Error 503: Service Unavailable", 503, True),
    ("ERROR: [youtube] abc: HTTP Error 404: Not Found", 404, False),
    ("ERROR: [youtube] abc: Video unavailable", None, False),
    ("ERROR: Unsupported URL: https://x", None, False),
])
def test_map_error(text: str, status, retryable: bool):
    err = _ytdlp.map_error(yt_dlp.utils.DownloadError(text), "youtube")
    assert err.status_code == status and err.retryable is retryable and err.provider == "youtube"
    assert not str(err).startswith("ERROR:")


def test_map_error_hints():
    assert "cookiefile" in _ytdlp.map_error(yt_dlp.utils.DownloadError("Sign in to confirm you're not a bot"), "youtube").hint
    assert "supportedsites" in _ytdlp.map_error(yt_dlp.utils.DownloadError("Unsupported URL: x"), "generic").hint


@pytest.mark.parametrize("raw, codec, bitrate, ext", [
    ("best", "best", None, None), ("", "best", None, None), ("native", "best", None, None),
    ("m4a", "m4a", None, "m4a"),
    ("mp3", "mp3", None, "mp3"), ("mp3_320", "mp3", 320, "mp3"), ("mp3:192", "mp3", 192, "mp3"),
    ("320k", "mp3", 320, "mp3"), ("128", "mp3", 128, "mp3"), ("opus", "opus", None, "opus"),
    ("vorbis", "vorbis", None, "ogg"), ("flac", "flac", None, "flac"), ("flac 320", "flac", None, "flac"),
])
def test_parse_quality(raw: str, codec: str, bitrate, ext: str):
    q = _ytdlp.parse_quality(raw, "youtube")
    assert (q.codec, q.bitrate, q.ext) == (codec, bitrate, ext)


def test_parse_quality_invalid():
    with pytest.raises(ProviderError, match="codec non supportato"):
        _ytdlp.parse_quality("wma", "youtube")
    with pytest.raises(ProviderError, match="qualità non riconosciuta"):
        _ytdlp.parse_quality("???", "youtube")
