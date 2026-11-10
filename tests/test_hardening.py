"""Test del brief di hardening (punti 1..9). Nessuna rete esterna: i server
HTTP finti girano su 127.0.0.1 (loopback permesso dal guard di conftest)."""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest
import yt_dlp

from core import stats
from sources import _ytdlp
from sources.base import ProviderError


@pytest.fixture
def opts(tmp_path: Path) -> dict:
    d = tmp_path / "ffmpeg"
    d.mkdir()
    (d / "ffmpeg.exe").write_bytes(b"")
    (d / "ffmpeg").write_bytes(b"")
    return {"ffmpeg_location": str(d)}


@pytest.fixture
def dead_server():
    """Server HTTP locale che risponde con lo status richiesto nel path: /404/x, /403/x."""

    class Handler(http.server.BaseHTTPRequestHandler):
        hits: list[str] = []

        def do_GET(self):  # noqa: N802
            Handler.hits.append(self.path)
            code = int(self.path.split("/")[1]) if self.path.split("/")[1].isdigit() else 404
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def log_message(self, *args):  # silenzioso
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", Handler.hits
    server.shutdown()
    server.server_close()


# ============================================ 1. Stream scaduto con URL morte
@pytest.mark.parametrize("status, kind", [(404, "missing"), (403, "forbidden")])
def test_expired_stream_dead_url_reextracts_once(dead_server, opts, tmp_path: Path, monkeypatch, status: int, kind: str):
    base_url, hits = dead_server
    hits.clear()
    extract_calls: list[str] = []

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        # la ri-estrazione "fresca" produce il file: se download_audio non
        # ri-estraesse, qui non si arriverebbe e il test fallirebbe
        extract_calls.append(target)
        out_dir = Path(self.params["outtmpl"]["default"]).parent
        (out_dir / "dead1.mp3").write_bytes(b"audio")
        return {"id": "dead1", "title": "x"}

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    stats.reset()
    info = {"id": "dead1", "title": "Dead", "ext": "mp3",
            "formats": [{"format_id": "0", "url": f"{base_url}/{status}/stream.mp3", "ext": "mp3", "acodec": "mp3", "vcodec": "none"}]}
    out = _ytdlp.download_audio(f"{base_url}/page", tmp_path / "o", "best", None, opts, "p", info=info)
    assert out.exists()
    assert len(extract_calls) == 1, "esattamente una ri-estrazione"
    assert any(h.startswith(f"/{status}/") for h in hits), "il vero downloader ha contattato la URL morta"
    assert stats.get("stream_reextract") == 1

    # seconda ri-estrazione che fallisce a sua volta -> errore permanente, nessun terzo tentativo
    extract_calls.clear()

    def extract_dead(self, target, download=False, **kw):  # noqa: ANN001
        extract_calls.append(target)
        raise yt_dlp.utils.DownloadError(f"ERROR: unable to download video data: HTTP Error {status}: dead")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_dead)
    with pytest.raises(ProviderError) as exc:
        _ytdlp.download_audio(f"{base_url}/page", tmp_path / "o2", "best", None, opts, "p", info=info)
    assert exc.value.kind == kind and not exc.value.retryable
    assert len(extract_calls) == 1 and stats.get("stream_reextract") == 2
    stats.reset()


# ============================================ 2. Bot-check come classe terminale
def test_needs_cookies_is_terminal_and_does_not_consume_attempts():
    from core import retry
    from sources.base import NeedsCookies

    err = _ytdlp.map_error(yt_dlp.utils.DownloadError("ERROR: HTTP Error 403: Forbidden. Sign in to confirm you're not a bot"), "p")
    assert isinstance(err, NeedsCookies) and err.kind == "need_cookies"
    assert err.retryable is False and err.retry_limit == 0 and err.status_code == 403
    assert "--cookies" in err.hint and "--cookies-from-browser" in err.hint and "cookiefile" in err.hint

    calls = {"n": 0}
    sleeps: list[float] = []

    def fn():
        calls["n"] += 1
        raise err

    cb = retry.CircuitBreaker("p", threshold=2, cooldown=60)
    with pytest.raises(NeedsCookies):
        retry.call_with_retry(fn, provider="p", breaker=cb, max_attempts=5, sleep=sleeps.append)
    assert calls["n"] == 1 and sleeps == []            # nessun retry, nessun backoff
    assert cb.failures == 0 and cb.unknown_failures == 0  # non conta per il breaker
    assert not retry.is_retryable(err)


def test_cookie_passthrough_cli_facade_and_ytdlp(fake_sources, tmp_path: Path, monkeypatch):
    from core import api, cli, registry

    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[general]\nout_dir = "out"\n[priority]\norder = ["fakeok"]\n', encoding="utf-8")

    # CLI: le opzioni finiscono nei default "*" del registry e quindi nel costruttore di ogni provider
    assert cli.main(["--config", str(cfg), "--cookies", str(cookies), "--cookies-from-browser", "firefox:default",
                     "--list-providers"]) == 0
    assert registry.OPTIONS["*"]["cookiefile"] == str(cookies.resolve())
    assert registry.OPTIONS["*"]["cookiesfrombrowser"] == "firefox:default"
    assert registry.load("fakeok").options["cookiefile"] == str(cookies.resolve())
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(cfg), "--cookies", str(tmp_path / "missing.txt"), "--list-providers"])
    assert exc.value.code == 2

    # façade
    registry.reset()
    from core.config import Config

    api_cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok"])
    tracks = api.resolve("https://fakeok.test/one", cfg=api_cfg, cookies=cookies, cookies_from_browser="chrome")
    assert tracks and registry.load("fakeok").options["cookiesfrombrowser"] == "chrome"
    with pytest.raises(api.ApiError, match="non trovato"):
        api.resolve("https://fakeok.test/one", cfg=api_cfg, cookies=tmp_path / "nope.txt")
    q = api.TaskQueue(api_cfg, cookies=cookies, autostart=False)
    assert registry.OPTIONS["*"]["cookiefile"] == str(cookies.resolve())
    q.stop()

    # opzioni yt-dlp: cookiefile vince; cookiesfrombrowser da solo viene risolto UNA volta per sessione
    # in un file Netscape temporaneo (brief successivo, punto 1): base_opts non passa più la tupla a yt-dlp
    opts = _ytdlp.base_opts({"cookiefile": str(cookies), "cookiesfrombrowser": "chrome+gnomekeyring:Profile 1::work"})
    assert opts["cookiefile"] == str(cookies) and "cookiesfrombrowser" not in opts
    monkeypatch.setattr(_ytdlp, "_extract_browser_cookies", lambda spec: yt_dlp.cookies.YoutubeDLCookieJar())
    _ytdlp._cleanup_browser_cookie_files()  # noqa: SLF001
    opts = _ytdlp.base_opts({"cookiesfrombrowser": "chrome+gnomekeyring:Profile 1::work"})
    assert "cookiesfrombrowser" not in opts and Path(opts["cookiefile"]).is_file()
    _ytdlp._cleanup_browser_cookie_files()  # noqa: SLF001
    assert _ytdlp.parse_browser_spec("chrome+gnomekeyring:Profile 1::work") == ("chrome", "Profile 1", "GNOMEKEYRING", "work")
    assert _ytdlp.parse_browser_spec("firefox") == ("firefox", None, None, None)
    assert _ytdlp.parse_browser_spec("edge:default") == ("edge", "default", None, None)
    with pytest.raises(ProviderError):
        _ytdlp.parse_browser_spec(":profile")
    spec = _ytdlp.parse_browser_spec("chrome+gnomekeyring:Profile 1::work")
    ydl = yt_dlp.YoutubeDL({"quiet": True, "cookiesfrombrowser": spec, "skip_download": True})
    assert ydl.params["cookiesfrombrowser"][0] == "chrome"  # yt-dlp accetta la tupla prodotta dal parser
    ydl.close()


# ============================================ 5. Pin del runtime
def test_single_exact_pin_in_runtime_and_range_only_in_dev():
    import re

    import yt_dlp.version as v

    root = Path(__file__).resolve().parent.parent
    files = {p.name: p.read_text(encoding="utf-8") for p in root.glob("requirements*.txt")}
    exact = {name: re.findall(r"^yt-dlp==(\S+)", text, re.M) for name, text in files.items()}
    exact = {k: v_ for k, v_ in exact.items() if v_}
    assert exact == {"requirements.txt": [v.__version__]}, "un solo pin esatto, nel file del runtime, = versione installata"
    assert "-r requirements.txt" in files["requirements-cli.txt"], "la CLI installa lo stesso file dell'app"
    assert re.search(r"^yt-dlp>=\S+,<\S+", files["requirements-dev.txt"], re.M), "intervallo solo in dev"
    assert "-r requirements.txt" not in files["requirements-dev.txt"]
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "--upgrade yt-dlp" not in docker and "-r requirements.txt" in docker


# ============================================ 4. SSRF
def _resolver_returning(*ips: str):
    import socket

    def resolver(host, port, family=0, type_=0, *args, **kwargs):  # noqa: ANN001
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    return resolver


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "172.16.5.5", "192.168.1.10", "::1", "fc00::1", "fe80::1", "0.0.0.0", "::ffff:10.0.0.1"])
def test_ssrf_private_addresses_are_rejected(ip: str, fake_sources, tmp_path: Path, monkeypatch):
    from core import api
    from core.config import Config

    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok"])
    monkeypatch.setattr(api, "RESOLVER", _resolver_returning(ip))
    with pytest.raises(api.ApiError) as exc:
        api.resolve("https://fakeok.test/one", cfg=cfg)
    assert exc.value.kind == "blocked" and "SSRF" in exc.value.hint
    assert not api.is_public_address(ip)
    # anche come IP letterale nell'URL, senza passare dal resolver
    literal = f"https://[{ip}]/x" if ":" in ip else f"https://{ip}/x"
    with pytest.raises(api.ApiError, match="non consentito"):
        api.check_public_url(literal, resolver=lambda *a, **k: pytest.fail("non deve risolvere un IP letterale"))


def test_ssrf_public_and_unresolvable_pass(fake_sources, tmp_path: Path, monkeypatch):
    from core import api
    from core.config import Config

    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok"])
    monkeypatch.setattr(api, "RESOLVER", _resolver_returning("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"))
    assert api.resolve("https://fakeok.test/one", cfg=cfg)          # IP pubblico: ammesso
    api.check_public_url("https://example.com/", resolver=_resolver_returning("8.8.8.8"))
    api.check_public_url("https://nope.invalid/", resolver=_resolver_returning())  # nessun indirizzo -> passa
    import socket

    def fail(*a, **k):
        raise socket.gaierror("nx")

    api.check_public_url("https://nope.invalid/", resolver=fail)   # non risolvibile -> passa
    # un solo indirizzo privato tra quelli risolti basta a rifiutare (DNS rebinding / dual answer)
    with pytest.raises(api.ApiError, match="blocked|non consentito"):
        api.check_public_url("https://mixed.example/", resolver=_resolver_returning("8.8.8.8", "10.0.0.1"))
    for bad in ("ftp://example.com/x", "http://localhost/x", "http://api.localhost/x", "http:///x"):
        with pytest.raises(api.ApiError):
            api.check_public_url(bad, resolver=_resolver_returning("8.8.8.8"))


def test_ssrf_task_is_failed_not_crashed(fake_sources, tmp_path: Path, monkeypatch):
    from core import api
    from core.config import Config

    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok"])
    monkeypatch.setattr(api, "RESOLVER", _resolver_returning("169.254.169.254"))
    q = api.TaskQueue(cfg)
    try:
        task = q.wait(q.submit("https://fakeok.test/one").id, timeout=30)
        assert task.state == "failed" and "169.254.169.254" in task.message and task.tracks_total == 0
    finally:
        q.stop()


# ============================================ 3 + 6. /audio via HTTP: auth, traversal, Range
@pytest.fixture
def http_app(fake_sources, tmp_path: Path, monkeypatch):
    """App FastAPI servita da TestClient (httpx), coda sui provider finti, credenziali da env."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from core import api
    from core.config import Config

    main = pytest.importorskip("app.main")
    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok", "fakemeta"])
    queue = api.TaskQueue(cfg)
    monkeypatch.setattr(main, "AUDIO_QUEUE", queue)
    monkeypatch.setattr(main.models, "sanitize_url", lambda u: u if u.startswith("http") else (_ for _ in ()).throw(ValueError("schema")))
    monkeypatch.setenv("GRABMEDIA_AUTH_USER", "tester")
    monkeypatch.setenv("GRABMEDIA_AUTH_PASSWORD", "s3cret")
    client = TestClient(main.app)
    yield main, queue, client, cfg
    queue.stop()


AUTH = ("tester", "s3cret")


def _done_task(queue, client, url="https://fakeok.test/one"):
    created = client.post("/audio", json={"url": url, "format": "best"}, auth=AUTH)
    assert created.status_code == 202
    task = queue.wait(created.json()["task_id"], timeout=30)
    assert task.state == "done" and task.files
    return task


def test_audio_requires_basic_auth(http_app):
    main, queue, client, _ = http_app
    for method, path in (("post", "/audio"), ("get", "/audio/" + "0" * 32), ("get", "/audio/" + "0" * 32 + "/file/0")):
        body = {"json": {"url": "https://fakeok.test/one"}} if method == "post" else {}
        r = getattr(client, method)(path, **body)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        r = getattr(client, method)(path, auth=("tester", "wrong"), **body)
        assert r.status_code == 401
    # credenziali da .htpasswd quando l'env non è impostato
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("GRABMEDIA_AUTH_USER")
        mp.delenv("GRABMEDIA_AUTH_PASSWORD")
        mp.setattr(main, "HTPASSWD_PATH", Path(main.__file__).resolve().parent.parent / "nginx" / ".htpasswd")
        assert client.get("/audio/" + "0" * 32, auth=("grabmedia", "grabmedia2026")).status_code == 404
        assert client.get("/audio/" + "0" * 32, auth=("grabmedia", "nope")).status_code == 401


def test_audio_file_range_206_and_416(http_app):
    _, queue, client, _ = http_app
    task = _done_task(queue, client)
    url = f"/audio/{task.id}/file/0"
    full = client.get(url, auth=AUTH)
    assert full.status_code == 200 and full.headers.get("accept-ranges") == "bytes"
    size = len(full.content)
    part = client.get(url, auth=AUTH, headers={"Range": "bytes=0-2"})
    assert part.status_code == 206 and part.content == full.content[:3]
    assert part.headers["content-range"] == f"bytes 0-2/{size}"
    bad = client.get(url, auth=AUTH, headers={"Range": f"bytes={size + 100}-"})
    assert bad.status_code == 416 and bad.headers["content-range"] == f"bytes */{size}"
    assert client.get(f"/audio/{task.id}/file/5", auth=AUTH).status_code == 404
    assert client.get(f"/audio/{task.id}/file/-1", auth=AUTH).status_code == 404


def test_audio_file_traversal_is_rejected(http_app, tmp_path: Path):
    from core import api

    _, queue, client, cfg = http_app
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    cases = {"abs": str(outside), "dots": str(cfg.out_dir / ".." / "outside.bin")}
    link = cfg.out_dir / "link.bin"
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
        cases["symlink"] = str(link)
    except (OSError, NotImplementedError):
        pass  # senza privilegi per i symlink su Windows: caso non verificabile qui
    for name, path in cases.items():
        tid = "f" * 31 + "0"
        queue._tasks[tid] = api.Task(id=tid, url="x", state="done", files=[path])  # noqa: SLF001
        r = client.get(f"/audio/{tid}/file/0", auth=AUTH)
        assert r.status_code == 400, name
    # percorso regolare dentro la root -> servito
    inside = cfg.out_dir / "ok.bin"
    inside.write_bytes(b"fine")
    tid = "e" * 32
    queue._tasks[tid] = api.Task(id=tid, url="x", state="done", files=[str(inside)])  # noqa: SLF001
    assert client.get(f"/audio/{tid}/file/0", auth=AUTH).content == b"fine"
    # un id malformato non arriva nemmeno alla logica
    assert client.get("/audio/../etc/passwd/file/0", auth=AUTH).status_code in (404, 422)


# ============================================ 7. Contratto errori della façade
def test_error_contract_ambiguous_case_all_tracks_fail(fake_sources, tmp_path: Path):
    """Caso ambiguo: TUTTE le tracce di una richiesta falliscono. Vince failures[]:
    nessuna ApiError, stato failed, failures popolate, message riassuntivo."""
    from core import api, registry
    from core.config import Config
    from sources.base import Track

    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok", "fakemeta"])
    meta = registry.load("fakemeta")
    meta.resolve = lambda url: [Track("fakemeta", "a", "A", ["X"], extra={"raise_nomatch": True}),
                                Track("fakemeta", "b", "B", ["X"], extra={"raise_nomatch": True})]
    q = api.TaskQueue(cfg)
    try:
        task = q.wait(q.submit("https://fakemeta.test/all-fail").id, timeout=30)
    finally:
        q.stop()
    assert task.state == "failed" and task.tracks_total == 2 and task.tracks_done == 0
    assert [f.kind for f in task.failures] == ["nomatch", "nomatch"] and "2 tracce su 2" in task.message
    # la stessa traccia singola via download(): non c'è una lista -> ApiError
    with pytest.raises(api.ApiError):
        api.download(Track("fakemeta", "a", "A", ["X"], extra={"raise_nomatch": True}), None, tmp_path / "a", cfg=cfg)
    # abort della richiesta: ApiError nelle funzioni, state failed senza failures nei task
    with pytest.raises(api.ApiError):
        api.resolve("https://fakeunavail.test/x", cfg=cfg)
    doc = api.__doc__ or ""
    assert "CONTRATTO ERRORI" in doc and "failures[]" in doc and "VINCE" in doc


# ============================================ 8. Runtime JS: validazione e avvio app
def test_js_runtime_env_pointing_to_missing_binary_warns_and_falls_back(tmp_path: Path, caplog):
    from core import jsruntime

    node = tmp_path / "node.exe"
    node.write_bytes(b"")
    which = lambda name: str(node) if name == "node" else None  # noqa: E731
    with caplog.at_level("WARNING", logger="grabmedia.jsruntime"):
        rt = jsruntime.detect(env={"YTDLP_JS_RUNTIME": "deno:" + str(tmp_path / "missing" / "deno.exe")}, which=which)
    assert rt == jsruntime.JsRuntime("node", str(node))
    assert any("non trovato" in r.message and "YTDLP_JS_RUNTIME" in r.message for r in caplog.records)
    assert jsruntime.detect(env={"YTDLP_JS_RUNTIME": "deno:/nope"}, which=lambda n: None) is None
    assert jsruntime.detect(env={"YTDLP_JS_RUNTIME": "node:" + str(node)}, which=lambda n: None) == jsruntime.JsRuntime("node", str(node))


def test_app_import_and_queue_without_js_runtime(monkeypatch, tmp_path: Path, caplog):
    import importlib
    import shutil

    from core import api, registry

    monkeypatch.delenv("YTDLP_JS_RUNTIME", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    main = importlib.import_module("app.main")  # l'import non deve sollevare
    assert not api.js_runtime_available()
    monkeypatch.setattr(main, "AUDIO_QUEUE", None)
    monkeypatch.setattr(main, "TaskQueue", lambda **kw: api.TaskQueue(
        api.config_mod.Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db"), autostart=False, **kw))
    with caplog.at_level("WARNING", logger="grabmedia.api"):
        queue = main.audio_queue()
    try:
        assert registry.load("youtube") is None
        assert "runtime JavaScript" in registry.UNAVAILABLE["youtube"] and "disabilitato" in registry.UNAVAILABLE["youtube"]
        assert any("youtube disabilitato" in r.message for r in caplog.records)
    finally:
        queue.stop()


# ============================================ 9. UI: avviso MP3 su Bandcamp
def test_ui_warns_about_bandcamp_mp3_128():
    html_text = Path(__file__).resolve().parent.parent.joinpath("frontend", "index.html").read_text(encoding="utf-8")
    assert 'id="bandcamp-mp3-hint"' in html_text and "128 kbps" in html_text and "non il file originale" in html_text
    assert "syncBandcampHint" in html_text and 'bandcamp\\.com' in html_text
