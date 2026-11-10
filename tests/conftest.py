"""Fixture comuni: nessuna rete, provider finti caricati in un ``sources/`` temporaneo."""

from __future__ import annotations

import socket
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sources  # noqa: E402
from core import registry  # noqa: E402


class _NoNetwork(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qualsiasi tentativo di aprire un socket fallisce: i test sono offline."""

    real_connect = socket.socket.connect

    def _is_loopback(address) -> bool:
        # asyncio su Windows crea una socketpair su 127.0.0.1 per l'event loop:
        # il loopback non è "rete" e resta permesso.
        try:
            host = address[0] if isinstance(address, tuple) else None
        except (TypeError, IndexError):
            return False
        return host in ("127.0.0.1", "::1", "localhost")

    def _deny(self, address, *args, **kwargs):  # noqa: ANN001
        if _is_loopback(address):
            return real_connect(self, address, *args, **kwargs)
        raise _NoNetwork("accesso alla rete bloccato nei test")

    def _deny_create(*args, **kwargs):  # noqa: ANN001
        raise _NoNetwork("accesso alla rete bloccato nei test")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny_create)


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset()
    yield
    registry.reset()


@pytest.fixture(autouse=True)
def offline_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Il guard anti-SSRF della façade risolve gli host via DNS: nei test gli
    host finti (``*.test``) risultano "non risolvibili" senza toccare la rete.
    I test SSRF sostituiscono esplicitamente il resolver."""
    from core import api

    def unresolvable(host, *args, **kwargs):  # noqa: ANN001
        raise socket.gaierror(f"stub: {host} non risolvibile nei test")

    monkeypatch.setattr(api, "RESOLVER", unresolvable)


@pytest.fixture(autouse=True)
def isolated_cookie_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """I file cookie del browser (anche finti) vanno in una directory del test,
    mai in quella reale dell'utente."""
    monkeypatch.setenv("GRABMEDIA_COOKIE_DIR", str(tmp_path_factory.mktemp("cookies")))


@pytest.fixture(autouse=True)
def no_js_runtime_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """La CLI richiede deno/node all'avvio: nei test (nessuna rete, yt-dlp
    mockato) si prosegue senza, come con --allow-no-js-runtime. I test del
    rilevamento rimuovono esplicitamente questa variabile."""
    monkeypatch.setenv("GRABMEDIA_ALLOW_NO_JS_RUNTIME", "1")
    monkeypatch.delenv("YTDLP_JS_RUNTIME", raising=False)


FAKE_OK = '''
from pathlib import Path
from sources.base import AudioSource, Track

CALLS = []

class FakeOk(AudioSource):
    name = "fakeok"

    def can_handle(self, url):
        return "fakeok.test" in url

    def resolve(self, url):
        n = 3 if "album" in url else 1
        return [
            Track("fakeok", f"{url}#{i}", f"Song {i}", ["Artist"], "Album",
                  duration_ms=1000 * i, track_no=i, year=2024)
            for i in range(1, n + 1)
        ]

    def download(self, track, dest, quality, progress_cb=None):
        CALLS.append((track.key, quality))
        if track.extra.get("raise_nomatch"):
            from sources.base import NoMatch
            raise NoMatch("nessun match", provider=self.name, query="q", score=0.1)
        if track.extra.get("raise_needs_cookies"):
            from sources._ytdlp import COOKIES_HINT
            from sources.base import NeedsCookies
            raise NeedsCookies("verifica anti-bot (403)", hint=COOKIES_HINT, provider=self.name, status_code=403)
        if progress_cb:
            progress_cb(50, "half")
            progress_cb(100, "done")
        out = Path(str(dest) + ".ogg")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ogg" + track.provider_id.encode())
        return out
'''

FAKE_BROKEN = '''
import this_dependency_does_not_exist  # noqa: F401
'''

FAKE_RAISES = '''
raise RuntimeError("bug nel modulo provider")
'''

FAKE_META = '''
from sources.base import MetadataSource, Track

class FakeMeta(MetadataSource):
    name = "fakemeta"
    audio_provider = "fakeok"

    def can_handle(self, url):
        return "fakemeta.test" in url

    def resolve(self, url):
        return [Track("fakemeta", "m1", "Meta Song", ["Meta Artist"], "Meta Album", track_no=7)]
'''

FAKE_FLAKY = '''
from pathlib import Path
from sources.base import AudioSource, ProviderError, Track

ATTEMPTS = {"n": 0}
FAIL_TIMES = {"n": 2}
STATUS = {"code": 503}

class FakeFlaky(AudioSource):
    name = "fakeflaky"

    def can_handle(self, url):
        return "fakeflaky.test" in url

    def resolve(self, url):
        return [Track("fakeflaky", "f1", "Flaky Song", ["Who"])]

    def download(self, track, dest, quality, progress_cb=None):
        ATTEMPTS["n"] += 1
        if ATTEMPTS["n"] <= FAIL_TIMES["n"]:
            raise ProviderError("temporaneo", status_code=STATUS["code"], provider=self.name)
        out = Path(str(dest) + ".mp3")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp3")
        return out
'''

FAKE_UNAVAIL = '''
from sources.base import AudioSource, ProviderUnavailable, Track

class FakeUnavail(AudioSource):
    name = "fakeunavail"

    def __init__(self, options=None):
        super().__init__(options)
        raise ProviderUnavailable("credenziali mancanti", hint="imposta FAKE_TOKEN", provider=self.name)

    def can_handle(self, url):
        return "fakeunavail.test" in url

    def resolve(self, url):
        return []

    def download(self, track, dest, quality, progress_cb=None):
        raise AssertionError("non deve essere chiamato")
'''

FAKE_GREEDY = '''
from sources.base import AudioSource, Track

class FakeGreedy(AudioSource):
    name = "fakegreedy"
    fallback = True  # provider di ultima istanza, come generic

    def can_handle(self, url):
        return True

    def resolve(self, url):
        return [Track("fakegreedy", "g1", "Greedy")]

    def download(self, track, dest, quality, progress_cb=None):
        raise AssertionError("non deve essere chiamato")
'''

FAKE_MODULES = {
    "fakeok": FAKE_OK,
    "fakebroken": FAKE_BROKEN,
    "fakeraises": FAKE_RAISES,
    "fakemeta": FAKE_META,
    "fakeflaky": FAKE_FLAKY,
    "fakegreedy": FAKE_GREEDY,
    "fakeunavail": FAKE_UNAVAIL,
}


@pytest.fixture
def fake_sources(tmp_path: Path):
    """Aggiunge una cartella temporanea di provider finti a ``sources.__path__``."""
    folder = tmp_path / "fake_sources"
    folder.mkdir()
    for name, code in FAKE_MODULES.items():
        (folder / f"{name}.py").write_text(textwrap.dedent(code), encoding="utf-8")
    sources.__path__.append(str(folder))
    yield folder
    sources.__path__.remove(str(folder))
    for name in list(sys.modules):
        if name.startswith("sources.fake"):
            del sys.modules[name]
