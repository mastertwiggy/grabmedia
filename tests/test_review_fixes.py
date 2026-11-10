"""Test dei comportamenti introdotti dalla revisione del blocco 4."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest
import yt_dlp

from core import retry
from sources import _ytdlp, bandcamp as bc_mod, matching
from sources.bandcamp import BandcampSource, extract_data_attrs, is_challenge_page
from sources.base import NoMatch, ProviderError, Track
from sources.soundcloud import SoundcloudSource

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def opts(tmp_path: Path) -> dict:
    d = tmp_path / "ffmpeg"
    d.mkdir()
    (d / "ffmpeg.exe").write_bytes(b"")
    (d / "ffmpeg").write_bytes(b"")
    return {"ffmpeg_location": str(d)}


@pytest.fixture
def fake_ydl(monkeypatch):
    """Mock di extract_info e process_ie_result (riuso dell'info)."""
    calls: list[tuple[str, str, dict]] = []
    handlers: dict[str, object] = {}

    def _write(self, info):
        out_dir = Path(self.params["outtmpl"]["default"]).parent
        ext = _ytdlp.CODEC_EXT[self.params["postprocessors"][0]["preferredcodec"]] or "mp3"
        (out_dir / f"{info['id']}.{ext}").write_bytes(b"audio")

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        calls.append(("extract", target, dict(self.params)))
        matches = [p for p in handlers if target.startswith(p)]
        if not matches:
            raise yt_dlp.utils.DownloadError(f"ERROR: nessun handler per {target}")
        result = handlers[max(matches, key=len)]
        info = result(target) if callable(result) else result
        if download:
            _write(self, info)
        return info

    def process_ie_result(self, info, download=False, **kw):  # noqa: ANN001
        calls.append(("process", info.get("id"), dict(self.params)))
        if info.get("_expired"):
            raise yt_dlp.utils.DownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        if download:
            _write(self, info)
        return info

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    monkeypatch.setattr(yt_dlp.YoutubeDL, "process_ie_result", process_ie_result)
    return {"calls": calls, "handlers": handlers}


# ------------------------------------------------------------- classificazione
@pytest.mark.parametrize("text, kind, retryable, limit, status", [
    ("ERROR: [soundcloud] 1: Unable to download JSON metadata: HTTP Error 429: Too Many Requests", "ratelimit", True, None, 429),
    ("ERROR: HTTP Error 502: Bad Gateway", "server", True, None, 502),
    ("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", "network", True, None, None),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot.", "auth", False, 0, None),
    ("ERROR: [soundcloud] x: This track is not available in your country", "forbidden", False, 0, None),
    ("ERROR: [soundcloud] x: Unable to download JSON metadata: HTTP Error 403: Forbidden", "forbidden", False, 0, 403),
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "auth", False, 0, None),
    ("ERROR: [youtube] x: Video unavailable", "missing", False, 0, None),
    ("ERROR: Unsupported URL: https://example.com/x", "unsupported", False, 0, None),
    ("ERROR: Postprocessing: ffprobe and ffmpeg not found", "ffmpeg", False, 0, None),
    ("ERROR: [DRM] The requested site is known to use DRM protection.", "drm", False, 0, None),
    ("ERROR: something nobody has seen before", "unknown", True, 1, None),
])
def test_classify_error_table(text: str, kind: str, retryable: bool, limit, status):
    assert _ytdlp.classify_error(text) == (kind, status)
    err = _ytdlp.map_error(yt_dlp.utils.DownloadError(text), "p")
    assert (err.kind, err.retryable, err.retry_limit, err.status_code) == (kind, retryable, limit, status)
    assert err.hint and not str(err).startswith("ERROR:")


def test_unknown_error_is_logged_raw(caplog):
    with caplog.at_level("WARNING", logger="grabmedia.ytdlp"):
        _ytdlp.map_error(yt_dlp.utils.DownloadError("ERROR: strange failure XYZ"), "p")
    assert any("NON classificato" in r.message and "strange failure XYZ" in r.message for r in caplog.records)


def test_safe_extract_handles_none_and_generators(opts: dict, monkeypatch):
    ydl = _ytdlp.make_ydl(opts, extract_flat=True, ignoreerrors=True)
    logger = ydl.params["logger"]

    def none_after_error(self, target, download=False, **kw):  # noqa: ANN001
        logger.error("ERROR: [soundcloud] x: HTTP Error 404: Not Found")
        return None

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", none_after_error)
    with pytest.raises(ProviderError) as exc:
        _ytdlp.safe_extract(ydl, "https://x", "p")
    assert exc.value.kind == "missing" and exc.value.status_code == 404

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", lambda self, t, download=False, **kw: None)
    with pytest.raises(ProviderError, match="nessuna informazione"):
        _ytdlp.safe_extract(_ytdlp.make_ydl(opts), "https://y", "p")

    gen_info = {"_type": "playlist", "entries": (e for e in [{"id": "1"}, {"id": "2"}])}
    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", lambda self, t, download=False, **kw: gen_info)
    info = _ytdlp.safe_extract(ydl, "https://z", "p")
    assert isinstance(info["entries"], list) and [e["id"] for e in info["entries"]] == ["1", "2"]

    def weird(self, t, download=False, **kw):  # noqa: ANN001
        raise RuntimeError("handler esploso")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", weird)
    with pytest.raises(ProviderError) as exc:
        _ytdlp.safe_extract(ydl, "https://w", "p")
    assert exc.value.kind == "unknown" and "handler esploso" in str(exc.value)


# ------------------------------------------------------------------- core
def test_retry_limit_unknown_errors_retry_once():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ProviderError("boh", retryable=True, retry_limit=1)

    with pytest.raises(ProviderError):
        retry.call_with_retry(fn, provider="p", max_attempts=10, sleep=lambda s: None)
    assert calls["n"] == 2


def test_breaker_ignores_permanent_errors():
    cb = retry.CircuitBreaker("p", threshold=2, cooldown=60)

    def permanent():
        raise ProviderError("gone", status_code=404)

    for _ in range(5):
        with pytest.raises(ProviderError):
            retry.call_with_retry(permanent, provider="p", breaker=cb, max_attempts=3, sleep=lambda s: None)
    assert not cb.is_open and cb.failures == 0

    def transient():
        raise ProviderError("down", status_code=503)

    with pytest.raises(ProviderError):
        retry.call_with_retry(transient, provider="p", breaker=cb, max_attempts=5, sleep=lambda s: None)
    assert cb.is_open


# --------------------------------------------------------------- matching
def test_pick_tie_break_prefers_uploader_then_duration():
    q, artist, title = "Forss Flickermood", "Forss", "Flickermood"
    a = {"id": "a", "title": "Forss - Flickermood", "uploader": "Random Reposts", "duration": 254}
    b = {"id": "b", "title": "Forss - Flickermood", "uploader": "Forss", "duration": 254}
    assert matching.pick(q, [a, b], 254_000, artist=artist, title=title)[0]["id"] == "b"
    assert matching.pick(q, [b, a], 254_000, artist=artist, title=title)[0]["id"] == "b"
    near = {"id": "near", "title": "Flickermood", "uploader": "Forss", "duration": 254}
    far = {"id": "far", "title": "Flickermood", "uploader": "Forss", "duration": 258}
    assert matching.pick(q, [far, near], 254_000, artist=artist, title=title)[0]["id"] == "near"


@pytest.mark.parametrize("bad", ["slowed", "sped up", "speed up", "nightcore", "8d", "bootleg"])
def test_pick_penalizes_speed_versions_even_with_same_duration(bad: str):
    original = {"id": "ok", "title": "Flickermood", "uploader": "Forss", "duration": 254}
    altered = {"id": "bad", "title": f"Flickermood ({bad})", "uploader": "Forss", "duration": 254}
    best, _ = matching.pick("Forss Flickermood", [altered, original], 254_000, artist="Forss", title="Flickermood")
    assert best["id"] == "ok"
    assert matching.pick("Forss Flickermood", [altered], 254_000, artist="Forss", title="Flickermood")[0] is None


# ------------------------------------------------------------- soundcloud
def test_set_with_broken_track_is_partial_and_keeps_positions(opts: dict, fake_ydl):
    sc = SoundcloudSource(opts)
    url = "https://soundcloud.com/forss/sets/soulhack"
    flat = {"_type": "playlist", "id": "77", "title": "Soulhack", "webpage_url": url,
            "entries": [{"_type": "url", "id": str(i), "url": f"https://api.soundcloud.com/tracks/{i}"} for i in range(1, 5)]}
    full = {**flat, "entries": [
        {"id": "1", "title": "One", "uploader": "Forss", "formats": [{"url": "https://cdn/1", "ext": "mp3"}]},
        None,  # brano 2 rotto
        {"id": "3", "title": "Three", "uploader": "Forss", "formats": [{"url": "https://cdn/3", "ext": "mp3"}]},
        {"id": "4", "title": "Four", "uploader": "Forss"},
    ]}
    fake_ydl["handlers"][url] = lambda t: flat if fake_ydl["calls"][-1][2].get("extract_flat") else full
    tracks = sc.resolve(url)
    assert [t.title for t in tracks] == ["One", "Three", "Four"]
    assert [t.track_no for t in tracks] == [1, 3, 4]  # posizioni dal set, non compresse
    assert tracks[0].extra["ytdlp_info"]["formats"] and "ytdlp_info" in tracks[2].extra

    full["entries"] = [None, None, None, {"id": "4", "title": "Four", "uploader": "Forss"}]
    sc2 = SoundcloudSource(opts)
    with pytest.raises(ProviderError) as exc:
        sc2.resolve(url)
    assert "3 brani su 4" in str(exc.value) and exc.value.kind == "missing"
    sc.close()
    sc2.close()


def test_set_download_reuses_info_and_falls_back_when_expired(opts: dict, fake_ydl, tmp_path: Path):
    sc = SoundcloudSource(opts)
    track = Track("soundcloud", "1", "One", ["Forss"], extra={
        "url": "https://soundcloud.com/forss/one",
        "ytdlp_info": {"id": "1", "title": "One", "formats": [{"url": "https://cdn/1", "ext": "mp3", "acodec": "mp3"}]},
    })
    final = sc.download(track, tmp_path / "one", "best")
    assert final.exists()
    kinds = [c[0] for c in fake_ydl["calls"]]
    assert kinds == ["process"]  # nessuna nuova estrazione

    fake_ydl["calls"].clear()
    track.extra["ytdlp_info"]["_expired"] = True
    fake_ydl["handlers"]["https://soundcloud.com/forss/one"] = {"id": "1", "title": "One"}
    final = sc.download(track, tmp_path / "one-again", "best")
    assert final.exists() and [c[0] for c in fake_ydl["calls"]] == ["process", "extract"]
    sc.close()


def test_large_set_does_not_keep_streams(opts: dict, fake_ydl):
    sc = SoundcloudSource({**opts, "max_full_tracks": 2})
    url = "https://soundcloud.com/forss/sets/big"
    ids = ["1", "2", "3"]
    flat = {"_type": "playlist", "id": "b", "title": "Big", "webpage_url": url,
            "entries": [{"_type": "url", "id": i, "url": f"https://api.soundcloud.com/tracks/{i}"} for i in ids]}
    full = {**flat, "entries": [{"id": i, "title": f"T{i}", "uploader": "F", "formats": [{"url": "u"}]} for i in ids]}
    fake_ydl["handlers"][url] = lambda t: flat if fake_ydl["calls"][-1][2].get("extract_flat") else full
    tracks = sc.resolve(url)
    assert len(tracks) == 3 and all("ytdlp_info" not in t.extra for t in tracks)
    sc.close()


def test_format_selector_prefers_native_codec():
    assert _ytdlp.parse_quality("best", "p").format_selector() == "bestaudio/best"
    assert _ytdlp.parse_quality("mp3", "p").format_selector() == "bestaudio[acodec^=mp3]/bestaudio/best"
    assert _ytdlp.parse_quality("m4a", "p").format_selector() == "bestaudio[acodec^=aac]/bestaudio/best"
    assert _ytdlp.parse_quality("opus", "p").format_selector() == "bestaudio[acodec^=opus]/bestaudio/best"


# --------------------------------------------------------------- bandcamp
def test_challenge_fixture_is_detected_and_cached(opts: dict, fake_ydl, monkeypatch):
    page = (FIXTURES / "bandcamp_challenge.html").read_text(encoding="utf-8")
    assert is_challenge_page(page) and extract_data_attrs(page) == {}
    fetches = {"n": 0}

    def fetch(url, options=None, **kw):
        fetches["n"] += 1
        return page

    monkeypatch.setattr(bc_mod, "fetch_text", fetch)
    bc = BandcampSource(opts)
    fake_ydl["handlers"]["https://nilsfrahm.bandcamp.com/"] = lambda t: {
        "id": "x", "title": "Solo", "_type": "playlist",
        "entries": [{"id": "1", "title": "Ode", "uploader": "Nils Frahm", "webpage_url": t.replace("album/solo", "track/ode"),
                     "formats": [{"url": "https://t4.bcbits.com/stream/1", "ext": "mp3", "acodec": "mp3"}]}],
    }
    tracks = bc.resolve("https://nilsfrahm.bandcamp.com/album/solo")
    assert bc.challenged and fetches["n"] == 1 and tracks[0].extra["downloadable"] is False
    assert tracks[0].extra["ytdlp_info"]["formats"]
    bc.resolve("https://nilsfrahm.bandcamp.com/album/other")
    assert fetches["n"] == 1  # con il flag attivo non si ritenta il fetch diretto
    bc.close()


def test_data_attrs_with_single_quotes_and_entities():
    data = {"artist": "A & B", "current": {"title": 'She said "hi"'}}
    page = f"<div data-tralbum='{html.escape(json.dumps(data), quote=True)}' data-band=\"{{}}\"></div>"
    assert extract_data_attrs(page)["tralbum"] == data
    page2 = f'<div data-tralbum="{html.escape(json.dumps(data), quote=True)}" data-embed="{html.escape(json.dumps({"album_title": "X"}), quote=True)}">'
    attrs = extract_data_attrs(page2)
    assert attrs["tralbum"] == data and attrs["embed"] == {"album_title": "X"}
    assert extract_data_attrs('<div data-tralbum="not json">') == {}


@pytest.mark.parametrize("quality, direct", [
    ("best", True), ("mp3", True), ("mp3_128", True), ("128k", True),
    ("mp3_320", False), ("m4a", False), ("opus", False), ("flac", False),
])
def test_bandcamp_direct_only_for_native_mp3_128(quality: str, direct: bool):
    assert BandcampSource.direct_allowed(quality) is direct


def test_bandcamp_docstring_states_stream_is_128():
    doc = " ".join((bc_mod.__doc__ or "").split())
    assert "mp3-128" in doc and "NON è il file originale" in doc
