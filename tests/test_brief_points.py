"""Test dei punti del brief post-revisione (1..7). Nessuna rete."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yt_dlp

from sources import _ytdlp
from sources.base import ProviderError


@pytest.fixture
def opts(tmp_path: Path) -> dict:
    d = tmp_path / "ffmpeg"
    d.mkdir()
    (d / "ffmpeg.exe").write_bytes(b"")
    (d / "ffmpeg").write_bytes(b"")
    return {"ffmpeg_location": str(d)}


# ============================================ 1. safe_extract: logger per chiamata
def test_concurrent_extractions_keep_their_own_error(opts: dict, monkeypatch):
    """Due estrazioni sovrapposte che falliscono entrambe: ognuna deve vedere
    il PROPRIO messaggio. Con un logger condiviso A leggerebbe l'errore di B."""
    first_logged = threading.Barrier(2, timeout=5)
    second_logged = threading.Barrier(2, timeout=5)

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        logger = self.params["logger"]
        if target == "fail-A":
            logger.error(f"ERROR: [x] {target}: Video unavailable")  # A logga per primo...
            first_logged.wait()
            second_logged.wait()                                        # ...e legge dopo che B ha loggato
        else:
            first_logged.wait()
            logger.error(f"ERROR: [x] {target}: HTTP Error 429: Too Many Requests")
            second_logged.wait()
        return None  # come con ignoreerrors: l'errore sta nel logger

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    base = _ytdlp.base_opts(opts, extract_flat=True, ignoreerrors=True)
    results: dict[str, ProviderError] = {}

    def run(target: str) -> None:
        try:
            _ytdlp.safe_extract(base, target, "p")
        except ProviderError as exc:
            results[target] = exc

    threads = [threading.Thread(target=run, args=(t,)) for t in ("fail-A", "fail-B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert set(results) == {"fail-A", "fail-B"}
    assert "fail-A" in results["fail-A"].message and results["fail-A"].kind == "missing"
    assert "fail-B" in results["fail-B"].message and results["fail-B"].kind == "ratelimit"


def test_safe_extract_builds_a_fresh_logger_per_call(opts: dict, monkeypatch):
    seen_loggers: list[object] = []

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        seen_loggers.append(self.params["logger"])
        return {"id": target, "title": target}

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    base = _ytdlp.base_opts(opts)
    _ytdlp.safe_extract(base, "a", "p")
    _ytdlp.safe_extract(base, "b", "p")
    assert len(seen_loggers) == 2 and seen_loggers[0] is not seen_loggers[1]
    assert all(isinstance(lg, _ytdlp.ExtractLogger) for lg in seen_loggers)
    assert base["logger"] is not seen_loggers[0]  # le opzioni condivise non vengono toccate


def test_no_module_level_error_state():
    assert not hasattr(_ytdlp, "LAST_ERROR")
    lg = _ytdlp.ExtractLogger()
    lg.error("x")
    assert _ytdlp.ExtractLogger().last_error is None  # ogni istanza parte pulita


# ============================================ 2. Fallback html.unescape
def test_unescape_fallback_is_reached_only_by_double_encoded_attributes(monkeypatch):
    """HTMLParser decodifica le entity una volta: il fallback serve solo a un
    attributo codificato DUE volte (``&amp;quot;``). Quote non chiuse o
    attributi malformati non lo raggiungono: producono semplicemente {}."""
    import html as html_mod
    import json

    from sources import bandcamp as bc_mod

    calls: list[str] = []
    real_unescape = html_mod.unescape
    monkeypatch.setattr(bc_mod.html, "unescape", lambda s: calls.append(s) or real_unescape(s))

    data = {"artist": "A & B", "trackinfo": []}
    once = html_mod.escape(json.dumps(data), quote=True)
    assert bc_mod.extract_data_attrs(f'<div data-tralbum="{once}"></div>') == {"tralbum": data}
    assert calls == []  # percorso normale: nessun fallback

    twice = html_mod.escape(once, quote=True)
    assert bc_mod.extract_data_attrs(f'<div data-tralbum="{twice}"></div>') == {"tralbum": data}
    assert len(calls) == 1 and "&quot;" in calls[0]  # raggiunto e risolutivo

    calls.clear()
    assert isinstance(bc_mod.extract_data_attrs(f'<div data-tralbum="{once}'), dict)  # quote non chiusa: nessun crash
    assert bc_mod.extract_data_attrs('<div data-tralbum=>') == {}
    assert bc_mod.extract_data_attrs("<div data-tralbum='{\"a\": 1'></div>") == {}  # JSON troncato
    # nei casi malformati il fallback viene tentato (JSON non valido) ma non produce un dict
    assert len(calls) == 1 and bc_mod._loads_lenient('{"a": 1') is None


# ============================================ 4. process_ie_result: pin e stream scaduti
def _ffmpeg_bin() -> Path | None:
    import os
    import shutil

    loc = os.environ.get("FFMPEG_LOCATION")
    if loc:
        p = Path(loc)
        for cand in (p, p / "ffmpeg.exe", p / "ffmpeg"):
            if cand.is_file():
                return cand
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


@pytest.mark.skipif(_ffmpeg_bin() is None, reason="serve ffmpeg reale per il test d'integrazione")
def test_integration_process_ie_result_with_real_ytdlp(tmp_path: Path, monkeypatch):
    """Esercita davvero ``YoutubeDL.process_ie_result`` (API interna pinnata):
    un info_dict con uno stream ``file://`` locale viene scaricato e
    post-processato senza mai chiamare ``extract_info``."""
    import subprocess

    from core import stats

    ffmpeg = _ffmpeg_bin()
    src = tmp_path / "tone.mp3"
    subprocess.run([str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(src)], check=True, timeout=120)
    assert src.stat().st_size > 0

    orig = _ytdlp.base_opts
    monkeypatch.setattr(_ytdlp, "base_opts", lambda *a, **k: {**orig(*a, **k), "enable_file_urls": True})
    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info",
                        lambda self, *a, **k: pytest.fail("extract_info non deve essere chiamato: l'info è riusato"))
    info = {
        "id": "local1", "title": "Local tone", "ext": "mp3", "webpage_url": "https://example.test/local1",
        "formats": [{"format_id": "0", "url": src.resolve().as_uri(), "ext": "mp3",
                     "acodec": "mp3", "vcodec": "none", "protocol": "file"}],
    }
    stats.reset()
    out = _ytdlp.download_audio("https://example.test/local1", tmp_path / "out" / "tone", "best", None,
                                {"ffmpeg_location": str(ffmpeg.parent)}, "p", info=info)
    assert out.exists() and out.stat().st_size > 0 and out.suffix == ".mp3"
    assert stats.get("stream_reextract") == 0


def test_expired_stream_reextracts_once_and_is_counted(tmp_path: Path, monkeypatch):
    from core import stats

    calls: list[str] = []

    def process_ie_result(self, info, download=False, **kw):  # noqa: ANN001
        calls.append("process")
        raise yt_dlp.utils.DownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        calls.append("extract")
        out_dir = Path(self.params["outtmpl"]["default"]).parent
        (out_dir / "x1.mp3").write_bytes(b"audio")
        return {"id": "x1", "title": "x"}

    monkeypatch.setattr(yt_dlp.YoutubeDL, "process_ie_result", process_ie_result)
    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    d = tmp_path / "ffmpeg"
    d.mkdir()
    (d / "ffmpeg.exe").write_bytes(b"")
    stats.reset()
    info = {"id": "x1", "title": "x", "formats": [{"url": "https://cdn/expired"}]}
    out = _ytdlp.download_audio("https://x/1", tmp_path / "o", "best", None, {"ffmpeg_location": str(d)}, "p", info=info)
    assert out.exists() and calls == ["process", "extract"]  # una sola ri-estrazione
    assert stats.get("stream_reextract") == 1

    calls.clear()
    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info",
                        lambda self, *a, **k: (_ for _ in ()).throw(yt_dlp.utils.DownloadError("ERROR: HTTP Error 410: Gone")))
    with pytest.raises(ProviderError) as exc:
        _ytdlp.download_audio("https://x/1", tmp_path / "o2", "best", None, {"ffmpeg_location": str(d)}, "p", info=info)
    assert calls == ["process"] and exc.value.kind == "missing" and stats.get("stream_reextract") == 2

    from core.pipeline import Summary, format_plan

    assert "stream_reextract=2" in format_plan(Summary(), dry_run=False)
    stats.reset()


def test_ytdlp_requirement_is_pinned_to_a_range():
    text = Path(_ytdlp.__file__).parent.parent.joinpath("requirements-cli.txt").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("yt-dlp"))
    assert ">=" in line and "<" in line
    import yt_dlp.version as v

    low = line.split(">=")[1].split(",")[0]
    assert v.__version__ >= low  # la versione installata è nel range testato


# ============================================ 5. Runtime JavaScript
def test_js_runtime_detection_order_and_override(monkeypatch, tmp_path):
    from core import jsruntime

    found = {"deno": None, "node": "/usr/bin/node"}
    rt = jsruntime.detect(env={}, which=lambda n: found.get(n))
    assert rt == jsruntime.JsRuntime("node", "/usr/bin/node")
    found["deno"] = "/opt/deno"
    assert jsruntime.detect(env={}, which=lambda n: found.get(n)).name == "deno"  # deno prima di node
    assert jsruntime.detect(env={"YTDLP_JS_RUNTIME": "node"}, which=lambda n: found.get(n)) == jsruntime.JsRuntime("node", "/usr/bin/node")
    # brief di hardening, punto 8: un percorso esplicito deve esistere davvero (prima bastava la sintassi)
    deno_exe = tmp_path / "deno.exe"
    deno_exe.write_bytes(b"")
    bun_bin = tmp_path / "bun"
    bun_bin.write_bytes(b"")
    assert jsruntime.detect(env={"YTDLP_JS_RUNTIME": f"deno:{deno_exe}"}, which=lambda n: None) == jsruntime.JsRuntime("deno", str(deno_exe))
    assert jsruntime.detect(env={"YTDLP_JS_RUNTIME": str(bun_bin)}, which=lambda n: None) == jsruntime.JsRuntime("bun", str(bun_bin))
    assert jsruntime.detect(env={}, which=lambda n: None) is None


def test_js_runtime_missing_error_lists_three_install_commands(monkeypatch):
    from core import jsruntime

    monkeypatch.delenv("GRABMEDIA_ALLOW_NO_JS_RUNTIME", raising=False)
    with pytest.raises(jsruntime.JsRuntimeMissing) as exc:
        jsruntime.ensure(env={}, which=lambda n: None)
    text = str(exc.value)
    for cmd in ("brew install deno", "winget install DenoLand.Deno", "curl -fsSL https://deno.land/install.sh | sh"):
        assert cmd in text
    assert "--allow-no-js-runtime" in text
    assert jsruntime.ensure(allow_missing=True, env={}, which=lambda n: None) is None
    assert jsruntime.ensure(env={"GRABMEDIA_ALLOW_NO_JS_RUNTIME": "1"}, which=lambda n: None) is None


def test_cli_exits_with_instructions_without_runtime(monkeypatch, tmp_path: Path, capsys):
    import shutil

    from core import cli

    monkeypatch.delenv("GRABMEDIA_ALLOW_NO_JS_RUNTIME", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[general]\nout_dir = "out"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(cfg), "--list-providers"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "winget install DenoLand.Deno" in err and "brew install deno" in err and "deno.land/install.sh" in err
    # con il flag si prosegue
    assert cli.main(["--config", str(cfg), "--list-providers", "--allow-no-js-runtime"]) == 0


def test_fake_runtime_in_path_is_passed_to_ytdlp(monkeypatch, tmp_path: Path, capsys):
    import os
    import shutil

    from core import cli

    fake = tmp_path / "deno.exe"
    fake.write_bytes(b"")
    monkeypatch.delenv("GRABMEDIA_ALLOW_NO_JS_RUNTIME", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake) if name == "deno" else None)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[general]\nout_dir = "out"\n', encoding="utf-8")
    assert cli.main(["--config", str(cfg), "--list-providers"]) == 0
    assert os.environ["YTDLP_JS_RUNTIME"] == f"deno:{fake}"
    opts = _ytdlp.base_opts({"ffmpeg_location": str(tmp_path)})
    assert opts["js_runtimes"] == {"deno": {"path": str(fake)}}
    # yt-dlp accetta il parametro così com'è
    ydl = yt_dlp.YoutubeDL({**opts, "js_runtimes": opts["js_runtimes"]})
    assert "deno" in ydl.params["js_runtimes"]
    ydl.close()
    assert _ytdlp.js_runtime_param("node") == {"node": {}}
    assert _ytdlp.js_runtime_param(None) is None


# ============================================ 6. Façade
@pytest.fixture
def api_cfg(tmp_path: Path):
    from core.config import Config

    return Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok", "fakemeta"],
                  backoff_base=0.0, backoff_cap=0.0)


def test_facade_resolve_and_download(fake_sources, api_cfg, tmp_path: Path):
    from core import api

    tracks = api.resolve("https://fakeok.test/album", cfg=api_cfg)
    assert len(tracks) == 3 and tracks[0].provider == "fakeok"
    out = api.download(tracks[0], "128k", None, cfg=api_cfg)
    assert out == tmp_path / "out" / "Artist" / "Album" / "01 - Song 1.ogg" and out.exists()
    out2 = api.download(tracks[1], None, tmp_path / "custom" / "song2", cfg=api_cfg)
    assert out2 == tmp_path / "custom" / "song2.ogg"
    meta = api.resolve("https://fakemeta.test/x", cfg=api_cfg)[0]
    assert api.download(meta, "best", None, cfg=api_cfg).name == "07 - Meta Song.ogg"  # delega


def test_facade_never_leaks_provider_error(fake_sources, api_cfg, tmp_path: Path, monkeypatch):
    from core import api
    from sources.base import ProviderError, Track

    monkeypatch.setattr(api.registry, "available_modules", lambda: ["fakeok", "fakemeta", "fakebroken"])
    with pytest.raises(api.ApiError) as exc:
        api.resolve("https://nobody.example/x", cfg=api_cfg)
    assert not isinstance(exc.value, ProviderError) and "Nessun provider" in exc.value.message and exc.value.hint
    with pytest.raises(api.ApiError) as exc:
        api.resolve("https://x", cfg=api_cfg, provider="fakebroken")
    assert exc.value.provider == "fakebroken" and exc.value.to_dict()["message"]
    with pytest.raises(api.ApiError) as exc:
        api.download(Track("fakemeta", "nm", "No Match", ["X"], extra={"raise_nomatch": True}), None, tmp_path / "x", cfg=api_cfg)
    assert exc.value.kind is None or isinstance(exc.value.kind, str)
    with pytest.raises(api.ApiError, match="non disponibile"):
        api.download(Track("ghost", "1", "t"), None, tmp_path / "g", cfg=api_cfg)


def test_task_queue_end_to_end(fake_sources, api_cfg, tmp_path: Path):
    from core import api

    q = api.TaskQueue(api_cfg)
    try:
        task = q.submit("https://fakeok.test/album", fmt="best")
        assert task.state == "queued" and task.id and task.url == "https://fakeok.test/album"
        done = q.wait(task.id, timeout=30)
        assert done.state == "done" and done.provider == "fakeok"
        assert done.tracks_total == 3 and done.tracks_done == 3 and done.progress == 100.0
        assert len(done.files) == 3 and all(Path(f).exists() for f in done.files)
        assert done.failures == [] and done.message is None
        snapshot = done.to_dict()
        assert set(snapshot) >= {"id", "url", "provider", "state", "progress", "tracks_total", "tracks_done", "failures"}
        assert [t.id for t in q.list()] == [task.id]

        # resume: seconda volta le tracce esistono già -> done, tracks_done 3
        again = q.wait(q.submit("https://fakeok.test/album").id, timeout=30)
        assert again.state == "done" and again.tracks_done == 3

        # partial: album di 3 con una traccia senza match
        meta = api.registry.load("fakemeta")
        from sources.base import Track

        meta.resolve = lambda url: [
            Track("fakemeta", "a", "A", ["X"]), Track("fakemeta", "b", "B", ["X"], extra={"raise_nomatch": True}),
            Track("fakemeta", "c", "C", ["X"]),
        ]
        partial = q.wait(q.submit("https://fakemeta.test/p").id, timeout=30)
        assert partial.state == "partial" and partial.tracks_done == 2 and partial.tracks_total == 3
        assert len(partial.failures) == 1 and partial.failures[0].kind == "nomatch" and partial.failures[0].track == "X - B"
        assert "1 tracce su 3" in partial.message

        # failed: URL rivendicato da un provider non disponibile (ProviderError -> stato + messaggio)
        failed = q.wait(q.submit("https://fakeunavail.test/x").id, timeout=30)
        assert failed.state == "failed" and "fakeunavail" in failed.message and failed.tracks_total == 0

        # failed: tutte le tracce senza match
        meta.resolve = lambda url: [Track("fakemeta", "z", "Z", ["X"], extra={"raise_nomatch": True})]
        allfail = q.wait(q.submit("https://fakemeta.test/z").id, timeout=30)
        assert allfail.state == "failed" and allfail.tracks_done == 0 and len(allfail.failures) == 1
        assert q.get("nope") is None
    finally:
        q.stop()


def test_app_imports_only_the_facade():
    import re

    app_dir = Path(__file__).resolve().parent.parent / "app"
    for py in app_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+sources\b", text, re.M), f"{py.name} importa da sources/"
        for line in text.splitlines():
            if re.match(r"^\s*(from|import)\s+core\b", line):
                assert "core.api" in line or line.strip().startswith("from core.api"), f"{py.name}: {line.strip()}"


# ============================================ 7. Wiring UI (endpoint /audio dell'app, via façade)
@pytest.fixture
def app_main(fake_sources, api_cfg, monkeypatch):
    """L'app FastAPI con la coda audio costruita sui provider finti."""
    pytest.importorskip("fastapi")
    from core import api

    main = pytest.importorskip("app.main")
    queue = api.TaskQueue(api_cfg)
    monkeypatch.setattr(main, "AUDIO_QUEUE", queue)

    def no_dns_sanitize(url: str) -> str:  # sanitize_url reale risolve l'host via DNS (rete bloccata nei test)
        if not url.startswith(("http://", "https://")):
            raise ValueError("Solo URL http/https")
        return url

    monkeypatch.setattr(main.models, "sanitize_url", no_dns_sanitize)
    yield main, queue
    queue.stop()


def test_app_audio_endpoints_end_to_end(app_main, tmp_path: Path):
    import asyncio

    from fastapi import HTTPException

    main, queue = app_main
    created = asyncio.run(main.create_audio_task(main.AudioRequest(url="https://fakeok.test/album", format="mp3_128")))
    task_id = created["task_id"]
    assert created["state"] == "queued" and len(task_id) == 32
    queue.wait(task_id, timeout=30)
    payload = asyncio.run(main.get_audio_task(task_id))
    assert payload["state"] == "done" and payload["tracks_done"] == 3 and payload["progress"] == 100.0
    assert payload["download_urls"] == [f"/audio/{task_id}/file/{i}" for i in range(3)]
    assert payload["filenames"][0] == "01 - Song 1.ogg"
    # ordine dei brani, non di completamento (i download girano in parallelo)
    assert payload["filenames"] == sorted(payload["filenames"]) and len(set(payload["filenames"])) == 3
    response = asyncio.run(main.serve_audio_file(0, task_id))
    assert Path(response.path).name == "01 - Song 1.ogg" and Path(response.path).exists()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.serve_audio_file(7, task_id))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        asyncio.run(main.get_audio_task("0" * 32))

    # un URL non gestito: errore mappato in stato + messaggio, nessuna eccezione dei provider
    created = asyncio.run(main.create_audio_task(main.AudioRequest(url="https://fakeunavail.test/x")))
    queue.wait(created["task_id"], timeout=30)
    payload = asyncio.run(main.get_audio_task(created["task_id"]))
    assert payload["state"] == "failed" and "fakeunavail" in payload["message"] and payload["download_urls"] == []


def test_app_audio_request_validation(app_main):
    main, _ = app_main
    with pytest.raises(ValueError):
        main.AudioRequest(url="https://fakeok.test/x", format="mp3; rm -rf /")
    with pytest.raises(ValueError):
        main.AudioRequest(url="ftp://x/y")
    assert main.AudioRequest(url="https://fakeok.test/x", format="  ").format is None


def test_frontend_wires_the_audio_path():
    html_text = Path(__file__).resolve().parent.parent.joinpath("frontend", "index.html").read_text(encoding="utf-8")
    assert 'id="audio-mode"' in html_text
    for needle in ('fetch(API + "/audio"', '"/audio/" + taskId', "download_urls", "syncAudioMode"):
        assert needle in html_text, needle


# ============================================ 3. 403 e sconosciuti
@pytest.mark.parametrize("text, kind, retryable", [
    ("ERROR: [soundcloud] x: Unable to download JSON metadata: HTTP Error 403: Forbidden", "forbidden", False),
    ("ERROR: [soundcloud] x: This track is not available in your country (HTTP Error 403: Forbidden)", "forbidden", False),
    # brief di hardening, punto 2: il bot-check è una classe TERMINALE (prima era retryable)
    ("ERROR: [youtube] x: HTTP Error 403: Forbidden. Sign in to confirm you're not a bot", "need_cookies", False),
    ("ERROR: [youtube] x: HTTP Error 403: Forbidden. Use --cookies or --cookies-from-browser", "need_cookies", False),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies", "auth", False),  # senza 403: auth
])
def test_403_variants(text: str, kind: str, retryable: bool):
    err = _ytdlp.map_error(yt_dlp.utils.DownloadError(text), "p")
    assert err.kind == kind and err.retryable is retryable
    if kind == "need_cookies":
        assert err.hint.startswith("need_cookies") and err.status_code == 403
    assert _ytdlp.map_error(yt_dlp.utils.DownloadError("ERROR: mai visto prima"), "p").retry_limit == 1


def test_breaker_opens_on_fifth_consecutive_unknown_and_resets_on_classified():
    from core import retry

    cb = retry.CircuitBreaker("p", threshold=5, cooldown=60)

    def unknown():
        raise ProviderError("boh", retryable=True, retry_limit=1, kind="unknown")

    for i in range(4):
        with pytest.raises(ProviderError):
            retry.call_with_retry(unknown, provider="p", breaker=cb, max_attempts=1, sleep=lambda s: None)
    assert cb.unknown_failures == 4 and not cb.is_open

    def classified():
        raise ProviderError("gone", status_code=404, kind="missing")

    with pytest.raises(ProviderError):
        retry.call_with_retry(classified, provider="p", breaker=cb, max_attempts=1, sleep=lambda s: None)
    assert cb.unknown_failures == 0  # azzerato da un errore classificato

    for i in range(5):
        with pytest.raises(ProviderError):
            retry.call_with_retry(unknown, provider="p", breaker=cb, max_attempts=1, sleep=lambda s: None)
    assert cb.is_open
    with pytest.raises(retry.CircuitOpen):
        retry.call_with_retry(lambda: "ok", provider="p", breaker=cb, max_attempts=1, sleep=lambda s: None)

    cb2 = retry.CircuitBreaker("q", threshold=5, cooldown=60)
    for i in range(4):
        with pytest.raises(ProviderError):
            retry.call_with_retry(unknown, provider="q", breaker=cb2, max_attempts=1, sleep=lambda s: None)
    assert retry.call_with_retry(lambda: "ok", provider="q", breaker=cb2, max_attempts=1) == "ok"
    assert cb2.unknown_failures == 0  # azzerato da un successo
