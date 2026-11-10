"""Sessione a cookie dell'area "Provider audio": /login, /logout, /session,
middleware su /audio e sui download dei file, riquadro di accesso nella pagina."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from core import api  # noqa: E402
from core.config import Config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def web(fake_sources, tmp_path: Path, monkeypatch):
    main = pytest.importorskip("app.main")
    cfg = Config(out_dir=tmp_path / "out", state_db=tmp_path / "st.db", priority=["fakeok", "fakemeta"])
    queue = api.TaskQueue(cfg)
    monkeypatch.setattr(main, "AUDIO_QUEUE", queue)
    monkeypatch.setattr(main.models, "sanitize_url", lambda u: u)
    monkeypatch.setenv("GRABMEDIA_AUTH_USER", "tester")
    monkeypatch.setenv("GRABMEDIA_AUTH_PASSWORD", "s3cret")
    main._SESSIONS.clear()        # noqa: SLF001
    main._LOGIN_FAILURES.clear()  # noqa: SLF001
    yield main, queue, TestClient(main.app)
    queue.stop()
    main._SESSIONS.clear()        # noqa: SLF001
    main._LOGIN_FAILURES.clear()  # noqa: SLF001


def _login(client, user="tester", password="s3cret"):
    return client.post("/login", json={"username": user, "password": password})


# ------------------------------------------------------------------ login
def test_login_sets_httponly_strict_short_lived_cookie(web):
    main, _, client = web
    r = _login(client)
    assert r.status_code == 200
    assert r.json() == {"authenticated": True, "user": "tester", "expires_in": main.SESSION_TTL_SECONDS}
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{main.SESSION_COOKIE}=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert f"max-age={main.SESSION_TTL_SECONDS}" in lowered and main.SESSION_TTL_SECONDS <= 3600  # scadenza breve
    assert "secure" not in lowered  # http in locale; su https viene aggiunto (test sotto)
    token = client.cookies.get(main.SESSION_COOKIE)
    assert token and len(token) >= 40 and "tester" not in token and "s3cret" not in token  # solo un token casuale
    assert "s3cret" not in r.text


def test_login_cookie_is_secure_behind_https(web):
    main, _, _ = web
    https = TestClient(main.app, base_url="https://testserver")
    cookie = _login(https).headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_wrong_credentials_get_401_and_no_cookie(web):
    main, _, client = web
    for user, password in (("tester", "wrong"), ("nobody", "s3cret"), ("tester", " s3cret")):
        r = _login(client, user, password)
        assert r.status_code == 401 and r.json()["detail"] == "Utente o password errati"
        assert "set-cookie" not in r.headers
    assert client.get("/session").json() == {"authenticated": False, "user": None}
    assert client.post("/login", json={"username": "", "password": "x"}).status_code == 422
    assert client.post("/login", json={"username": "x"}).status_code == 422


def test_login_is_throttled_after_repeated_failures(web):
    main, _, client = web
    for _ in range(main.LOGIN_MAX_FAILURES):
        assert _login(client, "tester", "wrong").status_code == 401
    r = _login(client)  # anche con la password giusta: bisogna attendere
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    main._LOGIN_FAILURES.clear()  # noqa: SLF001 - finestra trascorsa
    assert _login(client).status_code == 200


def test_credentials_from_htpasswd_sha(web, monkeypatch, tmp_path: Path):
    main, _, client = web
    monkeypatch.delenv("GRABMEDIA_AUTH_USER")
    monkeypatch.delenv("GRABMEDIA_AUTH_PASSWORD")
    htpasswd = tmp_path / ".htpasswd"
    # {SHA} di "segreta" (formato di nginx/htpasswd -s)
    import base64
    import hashlib

    digest = base64.b64encode(hashlib.sha1(b"segreta").digest()).decode()
    htpasswd.write_text(f"# commento\nanna:{{SHA}}{digest}\n", encoding="utf-8")
    monkeypatch.setattr(main, "HTPASSWD_PATH", htpasswd)
    assert _login(client, "anna", "sbagliata").status_code == 401
    assert _login(client, "anna", "segreta").status_code == 200
    monkeypatch.setattr(main, "HTPASSWD_PATH", tmp_path / "manca")
    main._LOGIN_FAILURES.clear()  # noqa: SLF001
    assert _login(TestClient(main.app), "anna", "segreta").status_code == 401  # file assente: nessuno entra


# ------------------------------------------------- middleware su /audio
def test_middleware_protects_audio_and_files_only(web):
    main, queue, client = web
    for method, path in (("post", "/audio"), ("get", "/audio/" + "a" * 32), ("get", "/audio/" + "a" * 32 + "/file/0"),
                         ("post", "/audio/cookies/renew"), ("get", "/audio/qualcosa/di/inesistente")):
        r = getattr(client, method)(path)
        assert r.status_code == 401 and r.json() == {"detail": "Accesso richiesto", "login": "/login"}, path
    # il resto del sito non richiede accesso
    assert client.get("/").status_code == 200
    assert client.get("/session").status_code == 200
    assert client.get("/audiobook").status_code == 404  # prefisso simile ma non protetto: 404, non 401
    assert main.is_protected_path("/audio") and main.is_protected_path("/audio/x/file/0")
    assert not main.is_protected_path("/audiobook") and not main.is_protected_path("/download/x/y")
    # un cookie inventato non basta
    client.cookies.set(main.SESSION_COOKIE, "token-inventato")
    assert client.get("/audio/" + "a" * 32).status_code == 401


def test_full_flow_login_task_file_logout(web):
    main, queue, client = web
    assert client.post("/audio", json={"url": "https://fakeok.test/one", "format": "best"}).status_code == 401
    assert _login(client).status_code == 200
    assert client.get("/session").json() == {"authenticated": True, "user": "tester"}
    created = client.post("/audio", json={"url": "https://fakeok.test/one", "format": "best"})
    assert created.status_code == 202
    task = queue.wait(created.json()["task_id"], timeout=30)
    assert task.state == "done"
    payload = client.get(f"/audio/{task.id}").json()
    assert payload["state"] == "done" and payload["download_urls"] == [f"/audio/{task.id}/file/0"]
    # il download del file passa dal cookie (come farà il link nella pagina)
    file_response = client.get(payload["download_urls"][0])
    assert file_response.status_code == 200 and file_response.content.startswith(b"ogg")
    assert client.get(payload["download_urls"][0], headers={"Range": "bytes=0-2"}).status_code == 206
    # logout: cookie cancellato e sessione distrutta lato server
    token = client.cookies.get(main.SESSION_COOKIE)
    out = client.post("/logout")
    assert out.status_code == 200 and out.json() == {"authenticated": False}
    assert "max-age=0" in out.headers["set-cookie"].lower()
    assert client.get(f"/audio/{task.id}").status_code == 401
    assert client.get(payload["download_urls"][0]).status_code == 401
    replay = TestClient(main.app)
    replay.cookies.set(main.SESSION_COOKIE, token)  # il vecchio token non vale più
    assert replay.get(f"/audio/{task.id}").status_code == 401
    assert client.post("/logout").status_code == 200  # idempotente


def test_session_expires(web, monkeypatch):
    main, _, client = web
    assert _login(client).status_code == 200
    assert client.get("/audio/" + "a" * 32).status_code == 404  # autenticato
    token = client.cookies.get(main.SESSION_COOKIE)
    assert main.session_user(token) == "tester"
    real_time = main.time.time
    monkeypatch.setattr(main.time, "time", lambda: real_time() + main.SESSION_TTL_SECONDS + 1)
    # lato server: anche se un client ripresentasse il vecchio cookie, il token è scaduto e viene rimosso
    replay = TestClient(main.app)
    replay.cookies.set(main.SESSION_COOKIE, token)
    assert replay.get("/audio/" + "a" * 32).status_code == 401
    assert replay.get("/session").json()["authenticated"] is False
    assert main.session_user(token) is None and main._SESSIONS == {}  # noqa: SLF001
    # lato browser: il cookie ha Max-Age, quindi dopo la scadenza non viene nemmeno più inviato
    assert client.get("/audio/" + "a" * 32).status_code == 401


def test_sessions_are_independent(web):
    main, _, client = web
    other = TestClient(main.app)
    assert _login(client).status_code == 200 and _login(other).status_code == 200
    assert client.cookies.get(main.SESSION_COOKIE) != other.cookies.get(main.SESSION_COOKIE)
    client.post("/logout")
    assert other.get("/session").json()["authenticated"] is True


# ----------------------------------------------------------- pagina e nginx
def test_frontend_login_box_and_no_credential_storage():
    html_text = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    # riquadro nascosto di default, con utente, password, errore, stato connesso e uscita
    assert re.search(r'<section class="card hidden" id="auth-card"', html_text)
    for needle in ('id="auth-user"', 'type="password" id="auth-pass"', 'id="auth-error"', 'id="auth-connected"',
                   'id="auth-logout"', "Connesso come", "Utente o password errati."):
        assert needle in html_text, needle
    # compare su 401 di /audio, usa /login e /logout, verifica lo stato con /session
    assert "if (res.status === 401) { showLogin(url); return; }" in html_text
    assert 'fetch(API + "/login"' in html_text and 'fetch(API + "/logout"' in html_text and 'fetch(API + "/session")' in html_text
    assert 'passField.value = "";' in html_text  # la password non resta nel campo
    # mai credenziali nel browser storage: solo il cookie HttpOnly
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "btoa(", "Authorization"):
        assert forbidden not in html_text, forbidden


def test_nginx_routes_session_endpoints_and_drops_basic_on_audio_files():
    conf = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    assert re.search(r"location ~ \^/\(audio\|login\|logout\|session\|", conf)
    audio_files = conf.split("location ~ ^/audio/[0-9a-f]{32}/file/[0-9]+$ {", 1)[1].split("}", 1)[0]
    assert "auth_basic" not in audio_files  # protetti dal cookie di sessione nell'app
    downloads = conf.split("location ~ ^/download/[0-9a-f]{32}/.+$ {", 1)[1].split("}", 1)[0]
    assert "auth_basic" in downloads  # i download classici restano come prima


def test_app_has_no_basic_auth_left():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "HTTPBasic" not in source and "WWW-Authenticate" not in source and "require_audio_auth" not in source
