"""Programma desktop: logica del launcher e avvio reale (senza finestra) in un
processo separato. Nessuna rete esterna: solo 127.0.0.1."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "desktop"))

import grabmedia_desktop as gd  # noqa: E402


@pytest.fixture
def clean_env():
    """``prepare_environment`` scrive in os.environ: lo ripristino a fine test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def http_get(port: int, path: str) -> tuple[int, bytes]:
    """GET su 127.0.0.1 con un socket grezzo (il guard dei test blocca create_connection)."""
    with socket.socket() as s:
        s.settimeout(15)
        s.connect(("127.0.0.1", port))
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


# ------------------------------------------------------------------ modalità
@pytest.mark.parametrize("argv, mode", [
    ([], "gui"),
    (["--no-gui"], "nogui"),
    (["--no-gui", "--port", "8123"], "nogui"),
    (["--worker", "--parent-pid", "42"], "worker"),
    (["--self-check"], "selfcheck"),
    (["-m", "yt_dlp", "-J", "https://x"], "ytdlp"),
    (["-m", "altro"], "gui"),
    (["-psn_0_12345"], "gui"),  # argomento che macOS può passare alle .app
])
def test_mode_from_argv(argv, mode):
    assert gd.mode_from_argv(argv) == mode


def test_self_command_frozen_and_source(monkeypatch):
    monkeypatch.setattr(gd, "FROZEN", False)
    cmd = gd.self_command("--worker")
    assert cmd[0] == sys.executable and cmd[1].endswith("grabmedia_desktop.py") and cmd[2] == "--worker"
    monkeypatch.setattr(gd, "FROZEN", True)
    assert gd.self_command("-m", "yt_dlp") == [sys.executable, "-m", "yt_dlp"]  # impacchettato: l'exe è il programma


def test_ytdlp_mode_matches_how_the_site_invokes_it():
    """Il sito lancia yt-dlp come ``sys.executable -m yt_dlp``: il programma
    impacchettato deve riconoscere esattamente quella forma."""
    from app import downloader

    cmd = downloader.ytdlp_command()
    assert cmd[0] == sys.executable and gd.mode_from_argv(cmd[1:] + ["--version"]) == "ytdlp"


# ------------------------------------------------------------------ percorsi
def test_data_and_music_dirs(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GRABMEDIA_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("GRABMEDIA_MUSIC_DIR", str(tmp_path / "m"))
    assert gd.data_dir() == tmp_path / "d" and gd.music_dir() == tmp_path / "m"
    monkeypatch.delenv("GRABMEDIA_DATA_DIR")
    monkeypatch.delenv("GRABMEDIA_MUSIC_DIR")
    assert gd.data_dir().name == "GrabMedia" and gd.music_dir() == Path.home() / "Music" / "GrabMedia"
    for platform, fragment in (("win32", "GrabMedia"), ("darwin", "Application Support"), ("linux", "GrabMedia")):
        monkeypatch.setattr(gd.sys, "platform", platform)
        assert fragment in str(gd.data_dir())


def test_default_config_is_written_once_and_valid(monkeypatch, tmp_path: Path):
    from core import config

    monkeypatch.setenv("GRABMEDIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GRABMEDIA_MUSIC_DIR", str(tmp_path / "music con spazi"))
    path = gd.write_default_config(tmp_path / "data" / "config.toml")
    cfg = config.load(path)
    assert cfg.out_dir == (tmp_path / "music con spazi").resolve()
    assert cfg.state_db == (tmp_path / "data" / "state.sqlite3").resolve()
    assert cfg.priority == ["spotify_embed", "spotify"]
    # MP3 di default: il formato originale (.opus) molti lettori non lo aprono
    from sources import _ytdlp

    assert cfg.quality_default == "mp3_192"
    quality = _ytdlp.parse_quality(cfg.quality_default, "youtube")
    assert quality.codec == "mp3" and quality.bitrate == 192 and not quality.native
    assert cfg.options["spotify_embed"] == {"mode": "embed", "audio_provider": "youtube"}
    assert cfg.naming == "{artist}/{album}/{track_no:02d} - {title}"
    # non sovrascrive le modifiche dell'utente
    path.write_text(path.read_text(encoding="utf-8").replace('default = "mp3_192"', 'default = "best"'), encoding="utf-8")
    gd.write_default_config(path)
    assert config.load(path).quality_default == "best"


def test_provider_audio_page_always_asks_for_mp3():
    """Provider audio scarica in MP3: la pagina non chiede più il formato nativo
    (arrivavano file .opus) e mostra la scelta "Solo audio MP3" col bitrate."""
    page = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'function audioFormat() { return "mp3_" + selectedBitrate(); }' in page
    assert 'return "best"; // i formati video' not in page
    assert "if (audioMode.checked) selectMp3();" in page
    assert 'audioMode.addEventListener("change", syncAudioMode);' in page
    assert "scarica in MP3" in page


def test_config_path_from_environment(monkeypatch, tmp_path: Path):
    from core import config

    path = gd.write_default_config(tmp_path / "c.toml")
    monkeypatch.setenv("GRABMEDIA_CONFIG", str(path))
    assert config.load().source_path == path
    monkeypatch.setenv("GRABMEDIA_CONFIG", str(tmp_path / "manca.toml"))
    with pytest.raises(config.ConfigError, match="non trovato"):
        config.load()


def test_prepare_environment(clean_env, monkeypatch, tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ffmpeg", "deno"):
        (bin_dir / f"{name}{gd.EXE_SUFFIX}").write_bytes(b"")
    monkeypatch.setattr(gd, "bundled_bin_dir", lambda: bin_dir)
    os.environ["GRABMEDIA_DATA_DIR"] = str(tmp_path / "data")
    os.environ["GRABMEDIA_MUSIC_DIR"] = str(tmp_path / "music")
    os.environ["REDIS_URL"] = "redis://altrove:1/0"
    env = gd.prepare_environment(redis_port=45678)
    assert env["PATH"].startswith(str(bin_dir) + os.pathsep)
    assert env["FFMPEG_LOCATION"] == str(bin_dir)
    assert env["YTDLP_JS_RUNTIME"] == f"deno:{bin_dir / ('deno' + gd.EXE_SUFFIX)}"
    assert env["GRABMEDIA_NO_AUTH"] == "1" and env["REDIS_PORT"] == "45678" and env["REDIS_HOST"] == "127.0.0.1"
    assert "REDIS_URL" not in env  # non deve puntare a un Redis esterno
    assert Path(env["GRABMEDIA_CONFIG"]).is_file() and (tmp_path / "music").is_dir()
    assert env["DOWNLOAD_DIR"] == str(tmp_path / "data" / "cache")
    # il runtime indicato viene accettato dal rilevamento del core e passato a yt-dlp
    from core import jsruntime
    from sources import _ytdlp

    assert jsruntime.detect().name == "deno"
    assert _ytdlp.js_runtime_param(env["YTDLP_JS_RUNTIME"]) == {"deno": {"path": str(bin_dir / ("deno" + gd.EXE_SUFFIX))}}


def test_prepare_environment_without_bundled_tools(clean_env, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(gd, "bundled_bin_dir", lambda: None)
    os.environ["GRABMEDIA_DATA_DIR"] = str(tmp_path / "data")
    os.environ["GRABMEDIA_MUSIC_DIR"] = str(tmp_path / "music")
    os.environ.pop("FFMPEG_LOCATION", None)
    os.environ.pop("YTDLP_JS_RUNTIME", None)
    env = gd.prepare_environment(redis_port=1)
    assert "FFMPEG_LOCATION" not in env and "YTDLP_JS_RUNTIME" not in env  # si useranno quelli di sistema


def test_std_streams_are_replaced_when_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GRABMEDIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    gd.ensure_std_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("riga di prova")
    sys.stdout.flush()
    stream = sys.stdout
    monkeypatch.undo()
    stream.close()
    assert "riga di prova" in (tmp_path / "grabmedia.log").read_text(encoding="utf-8")


# ----------------------------------------------- accesso disattivato in locale
def test_no_auth_mode_is_opt_in_and_local_only(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    main = pytest.importorskip("app.main")
    monkeypatch.delenv("GRABMEDIA_NO_AUTH", raising=False)
    client = TestClient(main.app)
    assert main.auth_required() is True
    assert client.get("/audio/" + "a" * 32).status_code == 401
    assert client.get("/session").json() == {"authenticated": False, "user": None}
    monkeypatch.setenv("GRABMEDIA_NO_AUTH", "1")
    assert main.auth_required() is False
    assert client.get("/session").json() == {"authenticated": True, "user": None, "auth_required": False}
    # il launcher ascolta solo sul loopback e la pagina non mostra il riquadro
    source = (ROOT / "desktop" / "grabmedia_desktop.py").read_text(encoding="utf-8")
    assert 'host="127.0.0.1"' in source and "0.0.0.0" not in source
    page = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "data.auth_required === false" in page


def test_redis_commands_are_loaded_without_the_model_folder(monkeypatch, tmp_path: Path):
    """Nella .app di macOS ``fakeredis/model`` non esiste su disco e fakeredis apre
    ``model/../commands.json``: il programma non partiva (build reale su macOS)."""
    pytest.importorskip("fakeredis")
    import shutil

    from fakeredis.model import _command_info as info

    package = tmp_path / "fakeredis"
    package.mkdir()
    shutil.copy2(Path(info.__file__).parent.parent / "commands.json", package / "commands.json")
    monkeypatch.setattr(info, "__file__", str(package / "model" / "_command_info.pyc"))  # "model" non esiste
    monkeypatch.setattr(info, "_COMMAND_INFO", None)
    assert not (package / "model").exists()
    assert gd.preload_redis_commands() is True
    assert info.get_command_info(b"get") is not None  # tabella già pronta: fakeredis non riapre il file
    # struttura interna diversa o file assente: nessuna eccezione, si lascia fare a fakeredis
    monkeypatch.setattr(info, "_COMMAND_INFO", None)
    monkeypatch.setattr(info, "__file__", str(tmp_path / "altrove" / "model" / "x.pyc"))
    assert gd.preload_redis_commands() is False


def test_backend_is_ready_only_when_its_own_server_started():
    """Una porta aperta da un altro programma non vuol dire "pronto": con --port su
    una porta occupata l'avvio veniva dato per riuscito (visto davvero)."""
    import threading
    import types

    with socket.socket() as other:  # un altro programma occupa la porta
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        backend = gd.Backend(port=other.getsockname()[1])
        assert gd.port_open(backend.port) and backend.ready() is False  # niente server nostro
        finished = threading.Thread(target=lambda: None)
        finished.start()
        finished.join()
        backend._server, backend._thread = types.SimpleNamespace(started=False), finished
        assert backend.ready() is False and "occupata" in backend.error
        assert backend.wait_ready(timeout=1) is False

    # thread creato ma non ancora avviato: "sta partendo", non "morto" (falso errore visto davvero)
    backend = gd.Backend(port=1)
    backend._server, backend._thread = types.SimpleNamespace(started=False), threading.Thread(target=lambda: None)
    assert backend.ready() is False and backend.error is None

    backend = gd.Backend(port=1)
    running = threading.Thread(target=time.sleep, args=(0.5,), daemon=True)
    running.start()
    backend._server, backend._thread = types.SimpleNamespace(started=False), running
    assert backend.ready() is False and backend.error is None  # sta ancora partendo
    backend._server.started = True
    assert backend.ready() is True


def test_headless_mode_stops_cleanly_on_sigterm():
    """Su macOS/Linux "termina" arriva come SIGTERM: deve fermare server e worker
    come Ctrl+C, non lasciare il worker senza padre."""
    source = (ROOT / "desktop" / "grabmedia_desktop.py").read_text(encoding="utf-8")
    assert "signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)" in source
    with pytest.raises(KeyboardInterrupt):
        gd._raise_keyboard_interrupt(15, None)


def test_program_exits_even_with_threads_still_running(tmp_path: Path):
    """Dopo "Esci" il processo deve terminare davvero: i thread non daemon del
    Redis in memoria lo tenevano vivo in background."""
    script = tmp_path / "prova.py"
    script.write_text(
        "import sys, threading, time\n"
        f"sys.path.insert(0, {str(ROOT / 'desktop')!r})\n"
        "import grabmedia_desktop as gd\n"
        "threading.Thread(target=time.sleep, args=(600,)).start()  # non daemon\n"
        "print('fatto')\n"
        "gd.hard_exit(3)\n",
        encoding="utf-8",
    )
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3 and proc.stdout.strip() == "fatto"  # output svuotato prima di uscire
    source = (ROOT / "desktop" / "grabmedia_desktop.py").read_text(encoding="utf-8")
    assert "hard_exit(main())" in source


# ------------------------------------------------------------- autocontrollo
def test_self_check_reports_missing_pieces(clean_env, monkeypatch, tmp_path: Path):
    os.environ["GRABMEDIA_DATA_DIR"] = str(tmp_path / "data")
    os.environ["GRABMEDIA_MUSIC_DIR"] = str(tmp_path / "music")
    os.environ.pop("YTDLP_JS_RUNTIME", None)
    # il registro dei test contiene i provider finti: qui serve quello vero su disco
    from core import registry

    monkeypatch.setattr(registry, "available_modules", lambda: list(gd.REQUIRED_PROVIDERS))
    monkeypatch.setattr(gd, "bundled_bin_dir", lambda: None)
    report = gd.self_check()
    assert report["ok"] is False and report["ffmpeg"] is None and report["deno"] is None
    assert any("ffmpeg" in p for p in report["problems"]) and any("deno" in p for p in report["problems"])
    assert report["frontend"] is True and report["yt_dlp"] and report["yt_dlp_ejs"] is True
    assert report["redis"] is True  # il server Redis in memoria si crea davvero

    monkeypatch.setattr(gd, "_tool_version", lambda path, flag: "versione 1")
    monkeypatch.setattr(registry, "available_modules", lambda: ["youtube"])
    report = gd.self_check()
    assert report["ffmpeg"] == "versione 1"
    assert any(p.startswith("provider mancanti: bandcamp, generic, soundcloud, spotify, spotify_embed") for p in report["problems"])


def test_required_providers_are_the_real_ones():
    """L'elenco che l'autocontrollo pretende coincide con i provider su disco."""
    on_disk = {p.stem for p in (ROOT / "sources").glob("*.py")} - {"__init__", "base", "matching", "_ytdlp"}
    assert set(gd.REQUIRED_PROVIDERS) == on_disk


def test_tool_version_handles_missing_and_broken(tmp_path: Path):
    assert gd._tool_version(None, "--version") is None
    assert gd._tool_version(tmp_path / "non-esiste", "--version") is None
    assert "Python" in gd._tool_version(Path(sys.executable), "--version")
    assert gd._tool_version(Path(sys.executable), "--opzione-inesistente") is None  # exit != 0


# ------------------------------------------------------------ script di build
def test_build_lists_every_project_module():
    """I provider sono caricati per nome a runtime: se non sono elencati uno per
    uno PyInstaller li lascia fuori e "Provider audio" non trova nessun provider
    (successo davvero alla prima build)."""
    import build

    modules = build.project_modules()
    for package in ("sources", "core", "app"):
        on_disk = {package if p.stem == "__init__" else f"{package}.{p.stem}" for p in (ROOT / package).glob("*.py")}
        assert on_disk and on_disk <= set(modules)
    assert {"sources.spotify_embed", "sources.youtube", "sources.matching", "core.cli", "app.worker"} <= set(modules)


def test_build_pyinstaller_arguments(tmp_path: Path):
    import build

    tools = {"ffmpeg": tmp_path / "ffmpeg", "deno": tmp_path / "deno"}
    args = build.pyinstaller_args(tools)
    pairs = list(zip(args, args[1:]))
    assert args[0].endswith("grabmedia_desktop.py") and "--windowed" in args and "--onedir" in args
    assert ("--hidden-import", "sources.spotify_embed") in pairs and ("--collect-all", "yt_dlp_ejs") in pairs
    assert ("--collect-submodules", "rq") in pairs and ("--collect-data", "fakeredis") in pairs
    assert ("--add-binary", f"{tools['ffmpeg']}{os.pathsep}bin") in pairs
    assert ("--add-binary", f"{tools['deno']}{os.pathsep}bin") in pairs
    assert ("--add-data", f"{ROOT / 'frontend' / 'index.html'}{os.pathsep}frontend") in pairs
    # la cartella "bin" e "frontend" sono quelle che il launcher cerca nel pacchetto
    source = (ROOT / "desktop" / "grabmedia_desktop.py").read_text(encoding="utf-8")
    assert 'BUNDLE_ROOT / "bin"' in source


def test_build_names_and_deno_lookup(monkeypatch, tmp_path: Path):
    import build

    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(build.platform, "machine", lambda: "arm64")
    assert build.archive_name() == "GrabMedia-macos-arm64.zip" and build.built_path().name == "GrabMedia.app"
    assert build.built_executable().parts[-3:] == ("Contents", "MacOS", "GrabMedia")
    monkeypatch.setattr(build.platform, "machine", lambda: "x86_64")
    assert build.archive_name() == "GrabMedia-macos-x64.zip"
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build.platform, "machine", lambda: "AMD64")
    assert build.archive_name() == "GrabMedia-windows-x64.zip" and build.built_path().name == "GrabMedia"
    # build.sys è il modulo sys vero: la piattaforma finta va tolta prima di usare
    # altro codice (su macOS shutil.which, credendosi su Windows, andava in errore)
    monkeypatch.undo()

    deno = tmp_path / "deno-finto"
    deno.write_bytes(b"x")
    monkeypatch.setenv("GRABMEDIA_DENO", str(deno))
    assert build.find_deno() == deno.resolve()
    monkeypatch.setenv("GRABMEDIA_DENO", str(tmp_path / "manca"))
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(build.BuildError, match="deno non trovato"):
        build.find_deno()


def test_desktop_workflow_is_valid_and_tests_the_package():
    yaml = pytest.importorskip("yaml")
    path = ROOT / ".github" / "workflows" / "build-desktop.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = data[True] if True in data else data["on"]  # YAML legge "on" come booleano
    assert "workflow_dispatch" in triggers and triggers["push"]["tags"] == ["v*"]
    assert data["permissions"] == {"contents": "read"}
    build_job = data["jobs"]["build"]
    systems = {entry["os"]: entry["zip"] for entry in build_job["strategy"]["matrix"]["include"]}
    assert systems == {"windows-latest": "GrabMedia-windows-x64.zip", "macos-latest": "GrabMedia-macos-arm64.zip",
                       "macos-15-intel": "GrabMedia-macos-x64.zip"}
    assert build_job["strategy"]["fail-fast"] is False
    script = "\n".join(step.get("run", "") for step in build_job["steps"])
    assert "desktop/build.py" in script and "--self-check" in script and "--no-gui" in script
    assert '"redis":true' in script and '"auth_required":false' in script
    assert "--gui-smoke" in script and '"app_ready": true' in script and "/app" in script
    steps = build_job["steps"]
    last = steps[-1]
    # il pacchetto si carica per ultimo, solo se tutte le prove (finestra compresa) sono passate
    assert last["uses"].startswith("actions/upload-artifact") and last["with"]["name"] == "${{ matrix.zip }}"
    assert "if" not in last
    names = [step.get("name", "") for step in steps]
    assert names.index("Prova del pacchetto") < names.index("Prova della finestra") < len(steps) - 1
    photo = next(step for step in steps if step.get("name") == "Foto della finestra")
    assert photo["if"] == "always()"  # la foto serve soprattutto quando la prova fallisce
    assert data["jobs"]["release"]["permissions"] == {"contents": "write"}  # scrittura solo dove serve


# ------------------------------------------------------------ finestra nativa
class FakeWindow:
    def __init__(self, url: str | None) -> None:
        self.url = url

    def get_current_url(self):
        return self.url


def make_api(monkeypatch, tmp_path: Path, url: str | None = "http://127.0.0.1:4321/app") -> "gd.DesktopApi":
    monkeypatch.setenv("GRABMEDIA_MUSIC_DIR", str(tmp_path / "musica"))
    monkeypatch.setenv("GRABMEDIA_VIDEO_DIR", str(tmp_path / "video"))
    monkeypatch.setenv("GRABMEDIA_DATA_DIR", str(tmp_path / "dati"))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "dati" / "cache"))
    api = gd.DesktopApi("http://127.0.0.1:4321")
    api._window = FakeWindow(url)
    return api


@pytest.mark.parametrize("argv, mode", [(["--gui-smoke"], "guismoke"), (["--tk"], "tk")])
def test_window_modes(argv, mode):
    assert gd.mode_from_argv(argv) == mode


def test_video_dir_and_folder_labels(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GRABMEDIA_VIDEO_DIR", str(tmp_path / "v"))
    assert gd.video_dir() == tmp_path / "v"
    monkeypatch.delenv("GRABMEDIA_VIDEO_DIR")
    monkeypatch.setattr(gd.sys, "platform", "darwin")
    assert gd.video_dir() == Path.home() / "Movies" / "GrabMedia"
    monkeypatch.setattr(gd.sys, "platform", "win32")
    assert gd.video_dir() == Path.home() / "Videos" / "GrabMedia"
    home = Path.home()
    assert gd.folder_label(home / "Music" / "GrabMedia") == "Musica › GrabMedia"
    assert gd.folder_label(home / "Movies" / "GrabMedia") == "Filmati › GrabMedia"
    assert gd.folder_label(home / "Videos" / "GrabMedia") == "Video › GrabMedia"
    assert gd.folder_label(tmp_path / "altra" / "cartella") == "altra › cartella"


def test_unique_path(tmp_path: Path):
    target = tmp_path / "video.mp4"
    assert gd.unique_path(target) == target
    target.write_bytes(b"x")
    assert gd.unique_path(target).name == "video (2).mp4"
    (tmp_path / "video (2).mp4").write_bytes(b"x")
    assert gd.unique_path(target).name == "video (3).mp4"


def test_desktop_api_refuses_other_pages(monkeypatch, tmp_path: Path):
    """Se la finestra mostrasse un altro sito, quel sito non deve poter usare il ponte."""
    for url in ("https://example.com/", "http://127.0.0.1:9999/app", None, "http://127.0.0.1:4321.evil.test/"):
        api = make_api(monkeypatch, tmp_path, url)
        assert api.folders() == {} and api.paste() == "" and api.open_folder("music") is False
        assert api.reveal(str(tmp_path)) is False and "error" in api.save_job("a" * 32)
    api = gd.DesktopApi("http://127.0.0.1:4321")
    assert api.folders() == {}  # nessuna finestra collegata


def test_desktop_api_folders_and_reveal(monkeypatch, tmp_path: Path):
    api = make_api(monkeypatch, tmp_path)
    shown: list[Path] = []
    monkeypatch.setattr(gd, "reveal_in_file_manager", shown.append)
    info = api.folders()
    assert info["music"] == str(tmp_path / "musica") and info["video"] == str(tmp_path / "video")
    assert info["music_label"] == f"{tmp_path.name} › musica" and info["platform"] in ("mac", "windows", "linux")
    song = tmp_path / "musica" / "Artista" / "brano.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")
    assert api.reveal(str(song)) is True and shown == [song.resolve()]
    outside = tmp_path / "segreto.txt"
    outside.write_bytes(b"x")
    assert api.reveal(str(outside)) is False                                  # fuori dalle cartelle di GrabMedia
    assert api.reveal(str(tmp_path / "musica" / ".." / "segreto.txt")) is False  # niente scorciatoie con ".."
    assert api.reveal(str(tmp_path / "musica" / "manca.mp3")) is False
    assert api.reveal(None) is False and api.reveal("") is False
    assert shown == [song.resolve()]


def test_desktop_api_save_job_copies_video_once(monkeypatch, tmp_path: Path):
    from app import database

    api = make_api(monkeypatch, tmp_path)
    cache = tmp_path / "dati" / "cache" / ("b" * 32)
    cache.mkdir(parents=True)
    clip = cache / "Il mio video.mp4"
    clip.write_bytes(b"video")
    jobs = {"b" * 32: {"status": "completed", "filepath": str(clip)},
            "c" * 32: {"status": "running", "filepath": ""},
            "d" * 32: {"status": "completed", "filepath": str(tmp_path / "segreto.txt")}}
    (tmp_path / "segreto.txt").write_bytes(b"x")
    monkeypatch.setattr(database, "get_job", lambda job_id: jobs.get(job_id))
    (tmp_path / "video").mkdir()
    (tmp_path / "video" / "Il mio video.mp4").write_bytes(b"vecchio")  # nome già usato: non va sovrascritto

    first = api.save_job("b" * 32)
    assert Path(first["path"]).name == "Il mio video (2).mp4" and Path(first["path"]).read_bytes() == b"video"
    assert api.save_job("b" * 32) == first  # una volta sola, niente copie doppie
    assert (tmp_path / "video" / "Il mio video.mp4").read_bytes() == b"vecchio"
    assert "non ancora finito" in api.save_job("c" * 32)["error"]
    assert "non è più disponibile" in api.save_job("d" * 32)["error"]  # fuori dalla cache: rifiutato
    assert "sconosciuto" in api.save_job("../../etc")["error"]
    assert "sconosciuto" in api.save_job(42)["error"]


def test_read_clipboard_never_raises(monkeypatch):
    import types

    monkeypatch.setattr(gd.sys, "platform", "darwin")
    monkeypatch.setattr(gd.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="  https://x.test/a \n"))
    assert gd.read_clipboard() == "https://x.test/a"

    def broken(*a, **k):
        raise OSError("niente pbpaste")

    monkeypatch.setattr(gd.subprocess, "run", broken)
    assert gd.read_clipboard() == ""


def test_error_page_escapes_the_message():
    page = gd.error_html("<script>alert(1)</script>")
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "grabmedia.log" in page
    assert "Avvio di GrabMedia" in gd.LOADING_HTML


class SmokeWindow:
    """Finestra finta per la prova automatica: risponde come la pagina vera."""

    def __init__(self, items, reveal=True):
        self.items, self.reveal, self.scripts = items, reveal, []

    def evaluate_js(self, script):
        self.scripts.append(script)
        if "__grabmediaItems" in script:
            return json.dumps(self.items)
        if "aria-label" in script:
            return self.reveal
        return True


def test_smoke_download_checks_the_real_file(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GRABMEDIA_MUSIC_DIR", str(tmp_path / "musica"))
    monkeypatch.setenv("GRABMEDIA_VIDEO_DIR", str(tmp_path / "video"))
    monkeypatch.setattr(gd.time, "sleep", lambda s: None)
    song = tmp_path / "musica" / "brano.mp3"
    song.parent.mkdir()
    song.write_bytes(b"x")
    window = SmokeWindow([{"state": "done", "title": "Brano", "files": [str(song)], "error": ""}])
    result = gd._smoke_download(window, "audio|https://www.youtube.com/watch?v=abc", timeout=5)
    assert result["ok"] is True and result["in_folder"] and result["file_exists"] and result["reveal_button"]
    assert 'data-mode=\\"audio\\"' in window.scripts[0] or 'data-mode="audio"' in window.scripts[0]
    assert '"https://www.youtube.com/watch?v=abc"' in window.scripts[0]  # link passato come stringa JSON
    # file finito nella cartella sbagliata (audio nella cartella video): non va bene
    result = gd._smoke_download(SmokeWindow([{"state": "done", "files": [str(song)]}]), "video|https://x.test/v", timeout=5)
    assert result["ok"] is False and result["in_folder"] is False
    # download fallito o senza pulsante "Mostra": non va bene
    assert gd._smoke_download(SmokeWindow([{"state": "failed", "files": []}]), "audio|u", timeout=5)["ok"] is False
    assert gd._smoke_download(SmokeWindow([{"state": "done", "files": [str(song)]}], reveal=False),
                              "audio|u", timeout=5)["ok"] is False


def test_desktop_interface_is_served_and_self_contained():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    main = pytest.importorskip("app.main")
    response = TestClient(main.app).get("/app")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    page = response.text
    assert "<title>GrabMedia</title>" in page and "window.__grabmediaReady = true" in page
    # nessuna risorsa esterna: il programma deve funzionare anche offline
    import re

    external = [u for u in re.findall(r'(?:src|href)="(https?://[^"]+)"', page)]
    assert external == [] and "<script src" not in page and "@import" not in page
    # contratto con le API: audio sempre MP3 via /audio, video via /download e salvataggio nel programma
    assert 'format: "mp3_" + item.bitrate' in page and '"/audio"' in page and '"/download"' in page
    assert "api.save_job(item.remoteId)" in page and "api.reveal(item.files[0])" in page
    assert "prefers-color-scheme: dark" in page and "-apple-system" in page
    assert "Mostra nel Finder" in page and "Mostra nella cartella" in page


def test_build_includes_window_interface_and_icon(monkeypatch, tmp_path: Path):
    import build

    tools = {"ffmpeg": tmp_path / "ffmpeg", "deno": tmp_path / "deno"}
    for platform, modules, icon in (("win32", ["webview.platforms.winforms", "webview.platforms.edgechromium"], "icon-win.png"),
                                    ("darwin", ["webview.platforms.cocoa"], "icon-mac.png")):
        monkeypatch.setattr(build.sys, "platform", platform)
        args = build.pyinstaller_args(tools)
        pairs = list(zip(args, args[1:]))
        for module in modules:
            assert ("--hidden-import", module) in pairs
        assert ("--icon", str(build.ASSETS / icon)) in pairs
        assert ("--add-data", f"{ROOT / 'frontend' / 'app.html'}{os.pathsep}frontend") in pairs
        assert ("--exclude-module", "PyQt6") in pairs
    monkeypatch.undo()


@pytest.mark.parametrize("name", ["icon-mac.png", "icon-win.png"])
def test_icons_are_square_1024_png(name):
    data = (ROOT / "desktop" / "assets" / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    assert (width, height) == (1024, 1024)


# ------------------------------------------------------- avvio reale, senza finestra
def test_headless_launcher_serves_the_site(tmp_path: Path):
    pytest.importorskip("fakeredis")
    port = gd.free_port()
    env = dict(os.environ)
    env.update({"GRABMEDIA_DATA_DIR": str(tmp_path / "data"), "GRABMEDIA_MUSIC_DIR": str(tmp_path / "music"),
                "PYTHONIOENCODING": "utf-8"})
    for key in ("GRABMEDIA_NO_AUTH", "GRABMEDIA_CONFIG", "REDIS_URL"):
        env.pop(key, None)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "desktop" / "grabmedia_desktop.py"), "--no-gui", "--port", str(port)],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 90
        while time.time() < deadline and not gd.port_open(port):
            assert proc.poll() is None, proc.stdout.read().decode("utf-8", "replace")[-2000:]
            time.sleep(0.3)
        assert gd.port_open(port), "il server non è partito"
        status, body = http_get(port, "/")
        assert status == 200 and b"GrabMedia" in body and b'id="audio-mode"' in body
        status, body = http_get(port, "/health")
        assert status == 200 and json.loads(body) == {"status": "ok", "redis": True}  # Redis in memoria attivo
        status, body = http_get(port, "/session")
        assert json.loads(body) == {"authenticated": True, "user": None, "auth_required": False}
        status, _ = http_get(port, "/audio/" + "a" * 32)
        assert status == 404  # nessun accesso richiesto: task inesistente, non 401
        status, body = http_get(port, "/jobs")
        assert status == 200 and json.loads(body) == {"jobs": []}
        # dati dell'utente fuori dal progetto: configurazione nella cartella dati
        assert (tmp_path / "data" / "config.toml").is_file() and (tmp_path / "music").is_dir()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
