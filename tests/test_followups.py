"""Test dei punti rimasti dopo l'hardening (1..7). Nessuna rete esterna."""

from __future__ import annotations

import http.cookiejar
import os
from pathlib import Path

import pytest
import yt_dlp

from core import api, registry
from core.config import Config
from sources import _ytdlp, bandcamp as bc_mod
from sources.base import ProviderError, Track


@pytest.fixture
def opts(tmp_path: Path) -> dict:
    d = tmp_path / "ffmpeg"
    d.mkdir()
    (d / "ffmpeg.exe").write_bytes(b"")
    (d / "ffmpeg").write_bytes(b"")
    return {"ffmpeg_location": str(d)}


@pytest.fixture
def fake_browser_cookies(monkeypatch):
    """Sostituisce la lettura dal browser: conta le invocazioni e restituisce un jar con un cookie."""
    calls: list[tuple] = []

    def extract(spec):
        calls.append(spec)
        jar = yt_dlp.cookies.YoutubeDLCookieJar()
        jar.set_cookie(http.cookiejar.Cookie(
            0, "session", "abc123", None, False, "example.com", True, False, "/", True,
            False, 2000000000, False, None, None, {},
        ))
        return jar

    monkeypatch.setattr(_ytdlp, "_extract_browser_cookies", extract)
    _ytdlp._cleanup_browser_cookie_files()  # noqa: SLF001 - stato pulito per il test
    yield calls
    _ytdlp._cleanup_browser_cookie_files()  # noqa: SLF001


# ============================================ 1. cookiesfrombrowser: una risoluzione per sessione
def test_browser_cookies_resolved_once_for_three_extractions(opts, fake_browser_cookies, monkeypatch):
    calls = fake_browser_cookies
    seen_cookiefiles: list[str] = []

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        seen_cookiefiles.append(self.params.get("cookiefile"))
        assert "cookiesfrombrowser" not in self.params  # non si rilegge il browser a ogni YoutubeDL
        return {"id": target, "title": target}

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    options = {**opts, "cookiesfrombrowser": "firefox:default"}
    for target in ("a", "b", "c"):
        _ytdlp.safe_extract(_ytdlp.base_opts(options), target, "p")
    assert len(calls) == 1 and calls[0] == ("firefox", "default", None, None)
    assert len(set(seen_cookiefiles)) == 1 and seen_cookiefiles[0]
    path = Path(seen_cookiefiles[0])
    assert path.is_file() and "# Netscape HTTP Cookie File" in path.read_text(encoding="utf-8")
    assert "session" in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    # file cancellato -> alla prossima necessità si risolve di nuovo (e una sola volta)
    path.unlink()
    _ytdlp.safe_extract(_ytdlp.base_opts(options), "d", "p")
    _ytdlp.safe_extract(_ytdlp.base_opts(options), "e", "p")
    assert len(calls) == 2
    # un cookiefile esplicito vince e non tocca il browser
    assert _ytdlp.effective_cookiefile({"cookiefile": "x.txt", "cookiesfrombrowser": "chrome"}) == "x.txt"
    assert len(calls) == 2
    _ytdlp._cleanup_browser_cookie_files()  # noqa: SLF001
    assert not Path(seen_cookiefiles[-1]).exists() if seen_cookiefiles[-1] else True


def test_browser_cookie_resolution_failure_is_actionable(opts, monkeypatch):
    _ytdlp._cleanup_browser_cookie_files()  # noqa: SLF001

    def boom(spec):
        raise FileNotFoundError("profilo non trovato")

    monkeypatch.setattr(_ytdlp, "_extract_browser_cookies", boom)
    with pytest.raises(ProviderError) as exc:
        _ytdlp.effective_cookiefile({"cookiesfrombrowser": "chrome"})
    assert "chrome" in str(exc.value) and "--cookies" in exc.value.hint


# ============================================ 2. Coerenza cookie sul percorso diretto Bandcamp
def _jar_of(opener) -> http.cookiejar.CookieJar | None:
    for handler in opener.handlers:
        jar = getattr(handler, "cookiejar", None)
        if jar is not None:
            return jar
    return None


def test_bandcamp_direct_path_uses_cookies_file_and_browser(opts, tmp_path: Path, fake_browser_cookies):
    # --cookies: file Netscape passato al percorso urllib
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tTRUE\t2000000000\tidentity\tXYZ\n", encoding="utf-8")
    jar = _jar_of(bc_mod._opener({**opts, "cookiefile": str(cookies)}))  # noqa: SLF001
    assert jar is not None and {c.name for c in jar} == {"identity"}
    # solo --cookies-from-browser: il file Netscape temporaneo (risolto una volta) arriva anche a urllib
    jar = _jar_of(bc_mod._opener({**opts, "cookiesfrombrowser": "chrome"}))  # noqa: SLF001
    assert jar is not None and {c.name for c in jar} == {"session"}
    bc_mod._opener({**opts, "cookiesfrombrowser": "chrome"})  # noqa: SLF001
    assert len(fake_browser_cookies) == 1  # nessuna rilettura
    # nessuno dei due: comportamento invariato (nessun cookie handler)
    assert _jar_of(bc_mod._opener(opts)) is None  # noqa: SLF001
    # anche il provider istanziato con i default "*" del registry (CLI/façade) li riceve
    registry.configure({"*": {"cookiefile": str(cookies)}, "bandcamp": opts, "youtube": opts, "generic": opts, "soundcloud": opts})
    bc = registry.load("bandcamp")
    assert bc is not None and bc.options["cookiefile"] == str(cookies)


# ============================================ 3. Stato failed se falliscono tutte le tracce
def test_all_tracks_failed_means_state_failed(fake_sources, tmp_path: Path):
    """Già garantito da test_error_contract_ambiguous_case_all_tracks_fail (test_hardening.py);
    qui lo stesso con fallimenti "failed" e "nomatch" misti."""
    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok", "fakemeta"])
    meta = registry.load("fakemeta")
    meta.resolve = lambda url: [Track("fakemeta", "a", "A", ["X"], extra={"raise_nomatch": True}),
                                Track("fakemeta", "b", "B", ["X"], extra={"raise_needs_cookies": True})]
    q = api.TaskQueue(cfg)
    try:
        task = q.wait(q.submit("https://fakemeta.test/all").id, timeout=30)
    finally:
        q.stop()
    assert task.state == "failed" and task.tracks_done == 0 and len(task.failures) == 2
    assert {f.kind for f in task.failures} == {"nomatch", "need_cookies"}


# ============================================ 4. NeedsCookies fino alla UI
def test_needs_cookies_hint_survives_to_task_payload_and_ui(fake_sources, tmp_path: Path, monkeypatch):
    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok", "fakemeta"])
    meta = registry.load("fakemeta")
    meta.resolve = lambda url: [Track("fakemeta", "ok", "Ok", ["X"]),
                                Track("fakemeta", "nc", "Blocked", ["X"], extra={"raise_needs_cookies": True})]
    q = api.TaskQueue(cfg)
    try:
        task = q.wait(q.submit("https://fakemeta.test/mix").id, timeout=30)
        assert task.state == "partial"
        failure = next(f for f in task.failures if f.kind == "need_cookies")
        assert failure.hint and "--cookies-from-browser" in failure.hint and "--cookies" in failure.hint
        assert task.to_dict()["failures"][-1]["hint"] == failure.hint  # campo dedicato nel payload

        # fino all'app: il JSON dell'endpoint contiene l'hint
        main = pytest.importorskip("app.main")
        monkeypatch.setattr(main, "AUDIO_QUEUE", q)
        import asyncio

        payload = asyncio.run(main.get_audio_task(task.id))
        assert any(f.get("hint") and "--cookies-from-browser" in f["hint"] for f in payload["failures"])
    finally:
        q.stop()
    # e il frontend lo mostra (non lo appiattisce nel messaggio)
    html_text = Path(__file__).resolve().parent.parent.joinpath("frontend", "index.html").read_text(encoding="utf-8")
    assert "f.hint" in html_text and "t.hint" in html_text
    # abort della richiesta: kind/hint dedicati anche a livello di task
    task2 = api.TaskQueue(cfg)
    try:
        t = task2.wait(task2.submit("https://fakeunavail.test/x").id, timeout=30)
        assert t.state == "failed" and t.kind in (None, "unavailable", "usage") or t.state == "failed"
        assert t.hint and "--provider" in t.hint and t.to_dict()["hint"] == t.hint  # hint del dispatch, dedicato
        assert "credenziali mancanti" in t.message  # motivo del provider nel messaggio
    finally:
        task2.stop()


# ============================================ 5. Guard SSRF: una sola implementazione
def test_single_ssrf_implementation(monkeypatch):
    import inspect

    import app.models as models

    assert not hasattr(models, "_is_private_ip")
    src = inspect.getsource(models)
    assert "is_private" not in src and "getaddrinfo" not in src and "check_public_url(" in src
    root = Path(__file__).resolve().parent.parent
    offenders = [p for p in root.glob("**/*.py") if "tests" not in p.parts and p.name != "api.py"
                 and "is_link_local" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"range SSRF duplicati in: {offenders}"

    # l'app passa dalla funzione unica: resolver stub -> stesso esito dei test di core.api
    stub_private = lambda host, *a, **k: [(2, 1, 6, "", ("10.0.0.1", 0))]  # noqa: E731
    stub_public = lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]  # noqa: E731
    monkeypatch.setattr(api, "RESOLVER", stub_private)
    with pytest.raises(models.URLValidationError, match="non consentito"):
        models.sanitize_url("https://evil.example/x")
    monkeypatch.setattr(api, "RESOLVER", stub_public)
    assert models.sanitize_url("https://good.example/x") == "https://good.example/x"
    # l'app, a differenza della CLI, pretende un host risolvibile
    import socket

    def nx(host, *a, **k):
        raise socket.gaierror("nx")

    monkeypatch.setattr(api, "RESOLVER", nx)
    with pytest.raises(models.URLValidationError, match="non risolvibile"):
        models.sanitize_url("https://nope.invalid/x")
    api.check_public_url("https://nope.invalid/x", resolver=nx)  # la façade invece lascia passare
    with pytest.raises(models.URLValidationError, match="non consentito"):
        models.sanitize_url("http://127.0.0.1:8080/x")


# ============================================ 6. Pin di starlette
def test_starlette_pinned_exact_in_runtime():
    import re

    import starlette

    root = Path(__file__).resolve().parent.parent
    runtime = (root / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(rf"^starlette=={re.escape(starlette.__version__)}$", runtime, re.M)
    dev = (root / "requirements-dev.txt").read_text(encoding="utf-8")
    assert re.search(r"^starlette>=", dev, re.M)
    from starlette.responses import FileResponse

    assert hasattr(FileResponse, "_handle_single_range")  # il supporto Range su cui si basa il 206


# ============================================ 7. Messaggio YouTube disabilitato
def test_disabled_youtube_error_contains_install_instructions(monkeypatch, tmp_path: Path):
    import shutil

    monkeypatch.delenv("YTDLP_JS_RUNTIME", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    main = pytest.importorskip("app.main")
    monkeypatch.setattr(main, "AUDIO_QUEUE", None)
    monkeypatch.setattr(main, "TaskQueue", lambda **kw: api.TaskQueue(
        Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db"), **kw))
    queue = main.audio_queue()
    try:
        task = queue.wait(queue.submit("https://www.youtube.com/watch?v=dQw4w9WgXcQ").id, timeout=30)
    finally:
        queue.stop()
    assert task.state == "failed" and task.kind in ("unavailable", None) or task.state == "failed"
    text = (task.message or "") + " " + (task.hint or "")
    for cmd in ("brew install deno", "winget install DenoLand.Deno", "curl -fsSL https://deno.land/install.sh | sh"):
        assert cmd in text
    assert "YTDLP_JS_RUNTIME" in text and "youtube" in task.message
    assert "youtube" in registry.UNAVAILABLE and "winget install DenoLand.Deno" in registry.UNAVAILABLE["youtube"]
