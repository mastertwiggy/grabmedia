"""Ciclo di vita del file cookie, precedenza dell'hint, host non risolvibile."""

from __future__ import annotations

import http.cookiejar
import os
import signal
import time
from pathlib import Path

import pytest
import yt_dlp

from core import api, registry
from core.config import Config
from sources import _ytdlp
from sources.base import Track


@pytest.fixture
def fake_browser(monkeypatch):
    calls: list[tuple] = []

    def extract(spec):
        calls.append(spec)
        jar = yt_dlp.cookies.YoutubeDLCookieJar()
        jar.set_cookie(http.cookiejar.Cookie(0, "session", "v", None, False, "example.com", True, False, "/", True,
                                             False, 2000000000, False, None, None, {}))
        return jar

    monkeypatch.setattr(_ytdlp, "_extract_browser_cookies", extract)
    _ytdlp._cleanup_browser_cookie_files()  # noqa: SLF001
    yield calls
    _ytdlp._cleanup_browser_cookie_files()  # noqa: SLF001


# ============================================ 1. Ciclo di vita del file cookie
def test_cookie_dir_is_dedicated_and_private(fake_browser):
    path = Path(_ytdlp.browser_cookie_file("chrome"))
    assert path.parent == _ytdlp.cookie_dir() and path.parent == Path(os.environ["GRABMEDIA_COOKIE_DIR"])
    assert path.name.startswith(_ytdlp.COOKIE_FILE_PREFIX)
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600 and (path.parent.stat().st_mode & 0o777) == 0o700


def test_stale_cookie_file_is_reresolved_after_ttl(fake_browser):
    calls = fake_browser
    first = _ytdlp.browser_cookie_file("chrome")
    assert len(calls) == 1
    # entro le 6 ore: nessuna rilettura
    assert _ytdlp.browser_cookie_file("chrome", now=time.time() + _ytdlp.COOKIE_TTL_SECONDS - 60) == first
    assert len(calls) == 1
    # oltre la soglia: file considerato stantio, ri-risolto (nuovo file, vecchio rimosso)
    second = _ytdlp.browser_cookie_file("chrome", now=time.time() + _ytdlp.COOKIE_TTL_SECONDS + 60)
    assert len(calls) == 2 and second != first and not Path(first).exists() and Path(second).exists()
    # mtime vecchio sul disco (es. file di una sessione precedente ripreso): stesso esito
    old = time.time() - _ytdlp.COOKIE_TTL_SECONDS - 10
    os.utime(second, (old, old))
    third = _ytdlp.browser_cookie_file("chrome")
    assert len(calls) == 3 and third != second


def test_startup_sweep_removes_orphans_older_than_a_day(fake_browser):
    d = _ytdlp.cookie_dir()
    # file di processi morti (pid 999999xxx non esistono): nome con il pattern completo
    old_orphan = d / f"{_ytdlp.COOKIE_FILE_PREFIX}999999001-old.txt"
    fresh_orphan = d / f"{_ytdlp.COOKIE_FILE_PREFIX}999999002-fresh.txt"
    unrelated = d / "altro.txt"
    for p in (old_orphan, fresh_orphan, unrelated):
        p.write_text("x", encoding="utf-8")
    stale = time.time() - 2 * 86400
    os.utime(old_orphan, (stale, stale))
    os.utime(unrelated, (stale, stale))
    mine = Path(_ytdlp.browser_cookie_file("chrome"))
    os.utime(mine, (stale, stale))  # il file di QUESTA sessione non è un orfano, anche se vecchio
    removed = api.prepare_cookie_store()  # sweep + handler dei segnali, come all'avvio di CLI e code
    assert removed == 1
    assert not old_orphan.exists() and fresh_orphan.exists() and unrelated.exists() and mine.exists()


def test_sigterm_and_sigint_cleanup_chain_previous_handler(fake_browser, monkeypatch):
    _ytdlp._reset_signal_cleanup_for_tests()  # noqa: SLF001
    seen: list[int] = []
    previous_term = signal.signal(signal.SIGTERM, lambda s, f: seen.append(s))
    previous_int = signal.signal(signal.SIGINT, lambda s, f: seen.append(s))
    try:
        assert _ytdlp.install_signal_cleanup() is True
        path = Path(_ytdlp.browser_cookie_file("chrome"))
        assert path.exists()
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # come alla ricezione del segnale
        assert not path.exists() and seen == [signal.SIGTERM]  # file cancellato, handler precedente chiamato
        path = Path(_ytdlp.browser_cookie_file("chrome"))
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert not path.exists() and seen == [signal.SIGTERM, signal.SIGINT]
        assert _ytdlp.install_signal_cleanup() is True  # idempotente
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        _ytdlp._reset_signal_cleanup_for_tests()  # noqa: SLF001


def test_explicit_renewal_cli_facade_and_endpoint(fake_browser, fake_sources, tmp_path: Path, monkeypatch):
    calls = fake_browser
    first = Path(_ytdlp.browser_cookie_file("chrome"))
    orphan = _ytdlp.cookie_dir() / f"{_ytdlp.COOKIE_FILE_PREFIX}999999004-leftover.txt"  # pattern completo, pid morto
    orphan.write_text("x", encoding="utf-8")
    # façade: scarta i file di sessione E quelli rimasti nella directory
    assert api.renew_browser_cookies() == 2 and not first.exists() and not orphan.exists()
    second = Path(_ytdlp.browser_cookie_file("chrome"))
    assert len(calls) == 2 and second != first
    # CLI: --renew-cookies
    from core import cli

    cfg = tmp_path / "config.toml"
    cfg.write_text('[general]\nout_dir = "out"\n', encoding="utf-8")
    assert cli.main(["--config", str(cfg), "--renew-cookies", "--list-providers"]) == 0
    assert not second.exists()
    # endpoint dell'app, autenticato
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    main = pytest.importorskip("app.main")
    monkeypatch.setenv("GRABMEDIA_AUTH_USER", "t")
    monkeypatch.setenv("GRABMEDIA_AUTH_PASSWORD", "p")
    third = Path(_ytdlp.browser_cookie_file("chrome"))
    main._SESSIONS.clear()        # noqa: SLF001
    main._LOGIN_FAILURES.clear()  # noqa: SLF001
    client = TestClient(main.app)
    assert client.post("/audio/cookies/renew").status_code == 401  # senza sessione
    assert client.post("/login", json={"username": "t", "password": "p"}).status_code == 200
    r = client.post("/audio/cookies/renew")
    assert r.status_code == 200 and r.json()["renewed"] is True and r.json()["removed_files"] >= 1
    assert not third.exists()


# ============================================ 1b. File cookie per processo
def test_cookie_file_name_includes_pid(fake_browser):
    path = Path(_ytdlp.browser_cookie_file("chrome"))
    assert _ytdlp.cookie_file_pid(path) == os.getpid()
    assert path.name.startswith(f"{_ytdlp.COOKIE_FILE_PREFIX}{os.getpid()}-")
    assert _ytdlp.cookie_file_pid("grabmedia-cookies-old.txt") is None
    assert _ytdlp.cookie_file_pid("grabmedia-cookies-12-abc.txt") == 12


def test_sweep_skips_files_of_live_pids_and_removes_dead_ones(fake_browser):
    """Due pid simulati: lo sweep "visto" da uno non cancella il file dell'altro se è vivo."""
    d = _ytdlp.cookie_dir()
    alive_pid, dead_pid = 111111, 222222
    alive_file = d / f"{_ytdlp.COOKIE_FILE_PREFIX}{alive_pid}-abc.txt"
    dead_file = d / f"{_ytdlp.COOKIE_FILE_PREFIX}{dead_pid}-abc.txt"
    dead_fresh = d / f"{_ytdlp.COOKIE_FILE_PREFIX}{dead_pid}-def.txt"
    for p in (alive_file, dead_file, dead_fresh):
        p.write_text("x", encoding="utf-8")
    stale = time.time() - 2 * 86400
    os.utime(alive_file, (stale, stale))
    os.utime(dead_file, (stale, stale))
    liveness = {alive_pid: True, dead_pid: False}
    removed = _ytdlp.sweep_cookie_dir(alive=lambda pid: liveness.get(pid, False))
    assert removed == 1
    assert alive_file.exists(), "il file di un processo vivo non si tocca anche se vecchio"
    assert not dead_file.exists() and dead_fresh.exists(), "morto e vecchio: via; morto ma recente: resta"
    # il processo corrente è sempre vivo
    assert _ytdlp.pid_alive(os.getpid()) is True and _ytdlp.pid_alive(0) is False


def test_sweep_leaves_foreign_files_untouched_in_overridden_dir(fake_browser, tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    monkeypatch.setenv("GRABMEDIA_COOKIE_DIR", str(shared))
    foreign = {
        "notes.txt": "x",
        "grabmedia-cookies-old.txt": "x",            # prefisso giusto ma senza pid: NON nostro
        "grabmedia-cookies-123.txt": "x",            # pid senza random: NON nostro
        "grabmedia-cookies-abc-def.txt": "x",        # pid non numerico
        "grabmedia-cookies-123-def.json": "x",       # estensione diversa
        "other-cookies-123-def.txt": "x",
    }
    for name, content in foreign.items():
        p = shared / name
        p.write_text(content, encoding="utf-8")
        stale = time.time() - 3 * 86400
        os.utime(p, (stale, stale))
    sub = shared / f"{_ytdlp.COOKIE_FILE_PREFIX}999-dir.txt"
    sub.mkdir()  # una directory con nome valido: ignorata
    dead = shared / f"{_ytdlp.COOKIE_FILE_PREFIX}999999003-zzz.txt"
    dead.write_text("x", encoding="utf-8")
    os.utime(dead, (time.time() - 3 * 86400,) * 2)
    removed = _ytdlp.sweep_cookie_dir(alive=lambda pid: False)
    assert removed == 1 and not dead.exists()
    assert all((shared / name).exists() for name in foreign) and sub.exists()
    # rinnovo esplicito (sweep a età 0): stessa regola, i file estranei restano
    api.renew_browser_cookies()
    assert all((shared / name).exists() for name in foreign)


def test_signal_cleanup_removes_only_current_process_files(fake_browser):
    _ytdlp._reset_signal_cleanup_for_tests()  # noqa: SLF001
    d = _ytdlp.cookie_dir()
    other = d / f"{_ytdlp.COOKIE_FILE_PREFIX}333333-abc.txt"
    other.write_text("x", encoding="utf-8")
    mine = Path(_ytdlp.browser_cookie_file("chrome"))
    # anche se un file altrui finisse registrato (non succede), il cleanup lo salterebbe
    _ytdlp._BROWSER_COOKIE_FILES["fake-other"] = str(other)  # noqa: SLF001
    seen: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda s, f: seen.append(s))
    try:
        assert _ytdlp.install_signal_cleanup()
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)
        _ytdlp._reset_signal_cleanup_for_tests()  # noqa: SLF001
    assert not mine.exists() and other.exists() and seen == [signal.SIGTERM]
    other.unlink()


# ============================================ 2. Precedenza dell'hint su abort
def test_needs_cookies_abort_keeps_provider_hint(fake_sources, tmp_path: Path):
    from sources._ytdlp import COOKIES_HINT
    from sources.base import NeedsCookies

    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok", "fakemeta"])
    src = registry.load("fakeok")

    def resolve(url):
        raise NeedsCookies("verifica anti-bot (403)", hint=COOKIES_HINT, provider="fakeok", status_code=403)

    src.resolve = resolve
    # funzione: ApiError con l'hint del provider
    with pytest.raises(api.ApiError) as exc:
        api.resolve("https://fakeok.test/x", cfg=cfg)
    assert exc.value.kind == "need_cookies" and "--cookies-from-browser" in exc.value.hint
    # task: kind e hint dedicati, con il comando esatto
    q = api.TaskQueue(cfg)
    try:
        task = q.wait(q.submit("https://fakeok.test/x").id, timeout=30)
    finally:
        q.stop()
    assert task.state == "failed" and task.kind == "need_cookies"
    assert task.hint.startswith("need_cookies") and "--cookies <file Netscape>" in task.hint
    assert task.to_dict()["hint"] == task.hint
    # abort via dispatch (provider non disponibile): l'hint del provider viene PRIMA di quello del dispatch
    with pytest.raises(api.ApiError) as exc:
        api.resolve("https://fakeunavail.test/x", cfg=cfg)
    assert exc.value.hint.startswith("imposta FAKE_TOKEN") and "--provider" in exc.value.hint


# ============================================ 3. Host non risolvibile nella façade
def test_unresolvable_host_policy(fake_sources, tmp_path: Path, monkeypatch):
    import socket

    def nx(host, *a, **k):
        raise socket.gaierror("nx")

    doc = api.__doc__ or ""
    assert "HOST NON RISOLVIBILE" in doc and "LASCIA PASSARE" in doc and "require_resolvable" in doc
    # façade: passa (il provider darà il suo errore)
    api.check_public_url("https://nope.invalid/x", resolver=nx)
    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok"])
    monkeypatch.setattr(api, "RESOLVER", nx)
    assert api.resolve("https://fakeok.test/one", cfg=cfg)
    # app: rifiuta
    with pytest.raises(api.ApiError) as exc:
        api.check_public_url("https://nope.invalid/x", resolver=nx, require_resolvable=True)
    assert exc.value.kind == "unresolvable"
    import app.models as models

    with pytest.raises(models.URLValidationError, match="non risolvibile"):
        models.sanitize_url("https://nope.invalid/x")
