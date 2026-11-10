"""Programma desktop di GrabMedia (Windows e macOS).

Un solo eseguibile che fa tutto in locale, senza installare niente:

* finestra nativa (pywebview: WebKit su macOS, Edge WebView2 su Windows) con
  l'interfaccia da app ``frontend/app.html``: niente browser, niente barra degli indirizzi;
  se il motore web manca si ripiega sulla finestrella tkinter + browser;
* server web interno su 127.0.0.1 (porta libera), che serve interfaccia e API;
* Redis in memoria (fakeredis) e worker dei download come processo figlio;
* ffmpeg e deno inclusi nel pacchetto (cartella ``bin``), messi nel PATH;
* nessuna password: ascolta solo su 127.0.0.1 (``GRABMEDIA_NO_AUTH=1``);
* dati dell'utente fuori dal pacchetto: configurazione, stato e cache nella
  cartella dati dell'utente, musica in ``~/Music/GrabMedia``.

Modalità dello stesso eseguibile (servono perché, impacchettato, ``sys.executable``
non è Python):

* nessun argomento      -> finestra + server;
* ``--no-gui``          -> solo server (test e uso da terminale), con ``--port N``;
* ``--worker``          -> worker dei download (lanciato dal processo principale);
* ``--self-check``      -> controlla che il pacchetto sia completo (JSON, exit 0/1);
* ``--gui-smoke``       -> apre la finestra vera, controlla l'interfaccia e si chiude (exit 0/1);
* ``--tk``              -> finestrella semplice + browser (la stessa usata se manca il motore web);
* ``-m yt_dlp ARGS...`` -> yt-dlp, perché il sito lo invoca come ``python -m yt_dlp``.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "GrabMedia"
VERSION = "1.0.0"
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
if not FROZEN and str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))

EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


# ------------------------------------------------------------------ percorsi
def data_dir() -> Path:
    """Cartella dati dell'utente (override: ``GRABMEDIA_DATA_DIR``)."""
    override = os.environ.get("GRABMEDIA_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def music_dir() -> Path:
    """Dove finisce la musica di "Provider audio" (override: ``GRABMEDIA_MUSIC_DIR``)."""
    return Path(os.environ.get("GRABMEDIA_MUSIC_DIR") or Path.home() / "Music" / APP_NAME)


def video_dir() -> Path:
    """Dove finiscono i video (override: ``GRABMEDIA_VIDEO_DIR``): Filmati su macOS, Video su Windows."""
    override = os.environ.get("GRABMEDIA_VIDEO_DIR")
    if override:
        return Path(override)
    return Path.home() / ("Movies" if sys.platform == "darwin" else "Videos") / APP_NAME


# Nomi con cui Finder ed Esplora file mostrano le cartelle dell'utente in italiano
_HOME_FOLDER_NAMES = {"Music": "Musica", "Movies": "Filmati", "Videos": "Video"}


def folder_label(path: Path) -> str:
    """Etichetta breve per l'interfaccia, es. ``Musica › GrabMedia``."""
    parent = _HOME_FOLDER_NAMES.get(path.parent.name, path.parent.name) if path.parent.parent == Path.home() \
        else path.parent.name
    return f"{parent} › {path.name}" if parent else path.name


def bundled_bin_dir() -> Path | None:
    """Cartella con ffmpeg e deno inclusi: ``bin`` nel pacchetto, ``desktop/vendor/bin`` in sviluppo."""
    for candidate in (BUNDLE_ROOT / "bin", Path(__file__).resolve().parent / "vendor" / "bin"):
        if candidate.is_dir():
            return candidate
    return None


def bundled_tool(name: str) -> Path | None:
    bin_dir = bundled_bin_dir()
    if bin_dir is None:
        return None
    path = bin_dir / f"{name}{EXE_SUFFIX}"
    return path if path.is_file() else None


DEFAULT_CONFIG = """\
# Configurazione di GrabMedia (programma desktop). Puoi modificarla: viene riletta all'avvio.

[general]
out_dir = "{music}"
naming = "{{artist}}/{{album}}/{{track_no:02d}} - {{title}}"
overwrite = false
state_db = "{data}/state.sqlite3"

[priority]
order = ["spotify_embed", "spotify"]

[concurrency]
default = 2

[quality]
# mp3_192 = MP3 a 192 kbps (si apre ovunque). "best" = formato originale, senza conversione (spesso .opus).
default = "mp3_192"

[options.spotify_embed]
mode = "embed"
audio_provider = "youtube"

[options.spotify]
cache_path = "{data}/spotify_cache.sqlite3"
token_cache_path = "{data}/spotify_token.json"
unmatched_log_file = "{data}/unmatched.log"
"""


def write_default_config(path: Path) -> Path:
    """Scrive la configurazione di default se manca (percorsi assoluti dell'utente)."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            DEFAULT_CONFIG.format(music=music_dir().as_posix(), data=data_dir().as_posix()),
            encoding="utf-8",
        )
    return path


def prepare_environment(redis_port: int) -> dict[str, str]:
    """Imposta l'ambiente del processo (ereditato da worker e yt-dlp) e lo restituisce."""
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True)
    music_dir().mkdir(parents=True, exist_ok=True)
    env = os.environ
    bin_dir = bundled_bin_dir()
    if bin_dir is not None:
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        if bundled_tool("ffmpeg") is not None:
            env["FFMPEG_LOCATION"] = str(bin_dir)
        deno = bundled_tool("deno")
        if deno is not None:
            env["YTDLP_JS_RUNTIME"] = f"deno:{deno}"
    env["GRABMEDIA_NO_AUTH"] = "1"                      # solo 127.0.0.1: nessun accesso da fare
    env["GRABMEDIA_CONFIG"] = str(write_default_config(home / "config.toml"))
    env["GRABMEDIA_COOKIE_DIR"] = str(home / "cookies")
    env["DOWNLOAD_DIR"] = str(home / "cache")            # cache dei download classici (pulita dopo 24 ore)
    env["REDIS_HOST"] = "127.0.0.1"
    env["REDIS_PORT"] = str(redis_port)
    env.pop("REDIS_URL", None)
    return dict(env)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_std_streams() -> None:
    """Nel programma "senza console" stdout/stderr sono ``None``: logging e uvicorn
    si romperebbero. Si scrive su un file di log nella cartella dati."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path = data_dir() / "grabmedia.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            log_path.unlink()  # niente log infiniti
    except OSError:
        pass
    stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - vive quanto il processo
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


# ------------------------------------------------------------------ modalità
def mode_from_argv(argv: list[str]) -> str:
    """``"ytdlp"``, ``"worker"``, ``"nogui"`` oppure ``"gui"``."""
    if len(argv) >= 2 and argv[0] == "-m" and argv[1] == "yt_dlp":
        return "ytdlp"
    if "--worker" in argv:
        return "worker"
    if "--self-check" in argv:
        return "selfcheck"
    if "--gui-smoke" in argv:
        return "guismoke"
    if "--tk" in argv:
        return "tk"
    if "--no-gui" in argv:
        return "nogui"
    return "gui"


def self_command(*args: str) -> list[str]:
    """Comando per rilanciare questo stesso programma in un'altra modalità."""
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def run_ytdlp(args: list[str]) -> None:
    """Equivalente di ``python -m yt_dlp ARGS`` per il programma impacchettato."""
    ensure_std_streams()
    for stream in (sys.stdout, sys.stderr):
        try:  # il sito legge l'output come UTF-8 (nel programma impacchettato PYTHONIOENCODING è ignorata)
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    import yt_dlp

    sys.argv = ["yt-dlp", *args]
    yt_dlp.main()


def _exit_when_parent_dies(parent_pid: int) -> None:
    from sources._ytdlp import pid_alive

    while True:
        time.sleep(2)
        if not pid_alive(parent_pid):
            os._exit(0)  # il programma è stato chiuso (o è crollato): il worker non resta orfano


def run_worker(argv: list[str]) -> None:
    ensure_std_streams()
    if "--parent-pid" in argv:
        try:
            parent = int(argv[argv.index("--parent-pid") + 1])
            threading.Thread(target=_exit_when_parent_dies, args=(parent,), daemon=True).start()
        except (ValueError, IndexError):
            pass
    import redis

    redis.Redis.client_list = lambda self, *a, **k: []  # fakeredis non implementa CLIENT LIST
    import app.worker as worker_mod

    worker_mod.Worker = worker_mod.SimpleWorker  # niente fork nel programma desktop (macOS compreso)
    worker_mod.main()


def preload_redis_commands() -> bool:
    """Carica la tabella dei comandi del Redis in memoria con un percorso normalizzato.

    fakeredis apre ``.../fakeredis/model/../commands.json``. Nella ``.app`` di macOS
    la cartella ``model`` non esiste su disco (i moduli stanno nell'archivio) e su
    macOS/Linux un percorso che attraversa una cartella inesistente fallisce: il
    programma non partiva (visto nella build reale). Windows lo normalizza da sé.
    Restituisce ``True`` se la tabella è disponibile.
    """
    import json

    try:
        from fakeredis.model import _command_info as info

        if info._COMMAND_INFO is None:
            path = Path(info.__file__).parent.parent / "commands.json"
            with open(path, encoding="utf8") as fh:
                info._COMMAND_INFO = info._encode_obj(json.load(fh))
        return bool(info._COMMAND_INFO)
    except Exception:  # noqa: BLE001 - struttura interna cambiata: fakeredis farà a modo suo
        return False


# ------------------------------------------------------------- autocontrollo
REQUIRED_PROVIDERS = ("bandcamp", "generic", "soundcloud", "spotify", "spotify_embed", "youtube")


def _tool_version(path: Path | None, flag: str) -> str | None:
    """Prima riga della versione dello strumento incluso, ``None`` se manca o non parte."""
    if path is None:
        return None
    try:
        proc = subprocess.run(
            [str(path), flag], capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (proc.stdout or proc.stderr or "").strip().splitlines()
    return lines[0].strip() if proc.returncode == 0 and lines else None


def self_check() -> dict:
    """Stato del pacchetto: provider presenti, ffmpeg e deno inclusi e avviabili,
    yt-dlp con gli script per le firme, pagina del sito. ``ok`` riassume tutto."""
    prepare_environment(redis_port=0)
    report: dict = {"version": VERSION, "frozen": FROZEN, "problems": []}

    from core import jsruntime, registry

    report["providers"] = registry.available_modules()
    missing = sorted(set(REQUIRED_PROVIDERS) - set(report["providers"]))
    if missing:
        report["problems"].append(f"provider mancanti: {', '.join(missing)}")
    for name, flag in (("ffmpeg", "-version"), ("deno", "--version")):
        report[name] = _tool_version(bundled_tool(name), flag)
        if report[name] is None:
            report["problems"].append(f"{name} incluso mancante o non avviabile")
    runtime = jsruntime.detect()
    report["js_runtime"] = runtime.name if runtime else None
    if runtime is None:
        report["problems"].append("nessun runtime JavaScript rilevato")
    try:
        import yt_dlp.version

        report["yt_dlp"] = yt_dlp.version.__version__
    except Exception as exc:  # noqa: BLE001
        report["yt_dlp"] = None
        report["problems"].append(f"yt-dlp non importabile: {exc}")
    try:
        import yt_dlp_ejs  # noqa: F401

        report["yt_dlp_ejs"] = True
    except Exception:  # noqa: BLE001
        report["yt_dlp_ejs"] = False
        report["problems"].append("script yt-dlp-ejs mancanti (firme di YouTube)")
    # Redis in memoria: deve riuscire a creare un server vero (non basta importarlo)
    try:
        from fakeredis import TcpFakeServer

        preload_redis_commands()
        server = TcpFakeServer(("127.0.0.1", free_port()), server_type="redis", server_version=(7, 2))
        server.server_close()
        report["redis"] = True
    except Exception as exc:  # noqa: BLE001
        report["redis"] = False
        report["problems"].append(f"Redis in memoria non avviabile: {type(exc).__name__}: {exc}")
    try:
        from app.main import FRONTEND_APP, FRONTEND_INDEX

        report["frontend"] = FRONTEND_INDEX.is_file() and FRONTEND_APP.is_file()
    except Exception as exc:  # noqa: BLE001
        report["frontend"] = False
        report["problems"].append(f"sito non importabile: {exc}")
    if report["frontend"] is False and not any("sito" in p for p in report["problems"]):
        report["problems"].append("pagine dell'interfaccia mancanti")
    # Finestra nativa: il modulo del motore deve essere nel pacchetto. Se manca solo
    # WebView2 sul computer non è un difetto del pacchetto (si usa la finestra di riserva).
    try:
        report["window"] = native_engine()
    except ImportError as exc:
        report["window"] = None
        report["problems"].append(f"finestra nativa non inclusa: {exc}")
    except Exception as exc:  # noqa: BLE001
        report["window"] = f"riserva ({exc})"
    report["ok"] = not report["problems"]
    return report


def run_self_check() -> int:
    import json

    ensure_std_streams()
    report = self_check()
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["ok"] else 1


# -------------------------------------------------------------------- backend
class Backend:
    """Redis in memoria + worker (processo figlio) + server web (thread)."""

    def __init__(self, port: int | None = None) -> None:
        self.port = port or free_port()
        self.redis_port = free_port()
        self.error: str | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._worker: subprocess.Popen | None = None
        self._redis = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self._worker is not None or self._thread is not None:
            return  # già avviato (la finestra di riserva riusa lo stesso backend)
        try:
            env = prepare_environment(self.redis_port)
            from fakeredis import TcpFakeServer

            preload_redis_commands()
            self._redis = TcpFakeServer(("127.0.0.1", self.redis_port), server_type="redis", server_version=(7, 2))
            threading.Thread(target=self._redis.serve_forever, daemon=True, name="fakeredis").start()
            for _ in range(50):
                if port_open(self.redis_port):
                    break
                time.sleep(0.1)

            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._worker = subprocess.Popen(
                self_command("--worker", "--parent-pid", str(os.getpid())),
                env=env, creationflags=flags,
            )  # senza console il worker non eredita stdout: scrive anche lui in grabmedia.log

            import uvicorn

            from app.main import app

            config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None, access_log=False)
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
            thread.start()
            # assegnati solo a thread avviato: ready() non deve mai vedere un thread
            # creato ma non ancora partito (lo scambiava per un server già morto)
            self._server, self._thread = server, thread
        except Exception as exc:  # noqa: BLE001 - l'errore va mostrato nella finestra, non perso
            self.error = f"{type(exc).__name__}: {exc}"
            raise

    def ready(self) -> bool:
        """Pronto solo se è partito il NOSTRO server: una porta aperta non basta
        (potrebbe essere di un altro programma, visto con --port su una porta occupata)."""
        server, thread = self._server, self._thread
        if self.error is not None or server is None or thread is None:
            return False
        if getattr(server, "started", False):
            return True
        if thread.ident is not None and not thread.is_alive():  # uvicorn è uscito senza partire: di solito porta occupata
            self.error = f"il server non è partito (porta {self.port} occupata?)"
        return False

    def wait_ready(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready():
                return True
            if self.error:
                return False
            time.sleep(0.2)
        return False

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._worker is not None and self._worker.poll() is None:
            self._worker.terminate()
            try:
                self._worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._worker.kill()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._redis is not None:
            try:
                self._redis.shutdown()
            except Exception:  # noqa: BLE001
                pass


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ------------------------------------------------------------- finestra nativa
def read_clipboard() -> str:
    """Testo negli appunti di sistema ("" se vuoti o non leggibili)."""
    text = ""
    try:
        if sys.platform == "darwin":
            text = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3).stdout
        elif sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = wintypes.LPVOID
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            for _ in range(10):  # un altro programma può tenerli aperti per un attimo
                if user32.OpenClipboard(None):
                    break
                time.sleep(0.05)
            else:
                return ""
            try:
                handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
                pointer = kernel32.GlobalLock(handle) if handle else None
                if pointer:
                    try:
                        text = ctypes.wstring_at(pointer)
                    finally:
                        kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        else:
            text = subprocess.run(["xclip", "-o", "-selection", "clipboard"],
                                  capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return (text or "").strip()[:4096]


def system_dark_mode() -> bool:
    """Tema scuro del sistema: serve solo a evitare il lampo bianco all'apertura."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=3).stdout
            return out.strip().lower() == "dark"
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def reveal_in_file_manager(path: Path) -> None:
    """Apre Finder / Esplora file con il file selezionato."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(f'explorer /select,"{path}"')  # noqa: S602 - percorso controllato, niente shell
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def unique_path(path: Path) -> Path:
    """``nome.ext`` libero: se esiste già diventa ``nome (2).ext``, ``nome (3).ext``…"""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"troppi file con lo stesso nome: {path.name}")


class DesktopApi:
    """Funzioni del programma chiamabili dall'interfaccia (``window.pywebview.api``).

    Tutto ciò che non inizia con ``_`` è esposto alla pagina: per questo ogni
    metodo controlla che la finestra mostri la NOSTRA pagina (se per qualche
    motivo navigasse altrove, quel sito non potrebbe usare queste funzioni) e
    lavora solo dentro le cartelle di GrabMedia.
    """

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/") + "/"
        self._window = None
        self._saved: dict[str, str] = {}
        self._lock = threading.Lock()

    def _trusted(self) -> bool:
        if self._window is None:
            return False
        try:
            url = self._window.get_current_url() or ""
        except Exception:  # noqa: BLE001
            return False
        return url.startswith(self._base)

    @staticmethod
    def _roots() -> list[Path]:
        return [music_dir().resolve(), video_dir().resolve()]

    def folders(self) -> dict:
        if not self._trusted():
            return {}
        return {
            "platform": "mac" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "linux",
            "music": str(music_dir()), "music_label": folder_label(music_dir()),
            "video": str(video_dir()), "video_label": folder_label(video_dir()),
        }

    def reveal(self, path: str) -> bool:
        if not self._trusted() or not isinstance(path, str) or not path:
            return False
        target = Path(path).resolve()
        if not any(target.is_relative_to(root) for root in self._roots()) or not target.exists():
            return False
        reveal_in_file_manager(target)
        return True

    def open_folder(self, which: str) -> bool:
        if not self._trusted():
            return False
        open_folder(video_dir() if which == "video" else music_dir())
        return True

    def paste(self) -> str:
        return read_clipboard() if self._trusted() else ""

    def save_job(self, job_id: str) -> dict:
        """Copia il video finito dalla cache alla cartella Video/Filmati (una volta sola)."""
        if not self._trusted():
            return {"error": "non consentito"}
        from app import database

        if not isinstance(job_id, str) or not database.is_valid_job_id(job_id):
            return {"error": "download sconosciuto"}
        with self._lock:
            if job_id in self._saved and Path(self._saved[job_id]).exists():
                return {"path": self._saved[job_id]}
            job = database.get_job(job_id) or {}
            if job.get("status") != "completed":
                return {"error": "download non ancora finito"}
            source = Path(job.get("filepath") or "")
            cache = Path(os.environ.get("DOWNLOAD_DIR") or data_dir() / "cache").resolve()
            if not source.is_file() or not source.resolve().is_relative_to(cache):
                return {"error": "il file non è più disponibile (i download vengono tenuti 24 ore)"}
            try:
                target_dir = video_dir()
                target_dir.mkdir(parents=True, exist_ok=True)
                target = unique_path(target_dir / source.name)
                shutil.copy2(source, target)
            except OSError as exc:
                return {"error": f"impossibile salvare il file: {exc}"}
            self._saved[job_id] = str(target)
            return {"path": str(target)}


_PAGE_STYLE = """
<style>
  :root { color-scheme: light dark; --bg: #f5f5f7; --fg: #1d1d1f; --muted: #6e6e73; --accent: #007aff; }
  @media (prefers-color-scheme: dark) { :root { --bg: #1c1c1e; --fg: #f5f5f7; --muted: #98989d; --accent: #0a84ff; } }
  html, body { height: 100%; margin: 0; background: var(--bg); color: var(--fg); cursor: default;
    font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif; }
  body { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px;
    text-align: center; padding: 0 40px; -webkit-user-select: none; user-select: none; }
  .spin { width: 22px; height: 22px; border-radius: 50%; border: 2.5px solid rgba(128,128,128,.25);
    border-top-color: var(--accent); animation: r .8s linear infinite; }
  @keyframes r { to { transform: rotate(360deg); } }
  h1 { font-size: 15px; font-weight: 600; margin: 0; }
  p { margin: 0; color: var(--muted); max-width: 440px; -webkit-user-select: text; user-select: text; }
</style>"""

LOADING_HTML = f"<!doctype html><html><head><meta charset='utf-8'>{_PAGE_STYLE}</head>" \
               "<body><div class='spin'></div><p>Avvio di GrabMedia…</p></body></html>"


def error_html(message: str) -> str:
    import html

    log_path = html.escape(str(data_dir() / "grabmedia.log"))
    return (f"<!doctype html><html><head><meta charset='utf-8'>{_PAGE_STYLE}</head><body>"
            "<h1>GrabMedia non è riuscito ad avviarsi</h1>"
            f"<p>{html.escape(message)}</p><p>Dettagli nel file {log_path}</p></body></html>")


# Menu e finestre di sistema in italiano, con le parole che usa macOS
# ("Informazioni su", "Nascondi" ed "Esci da" sono seguiti dal nome del programma).
LOCALIZATION_IT = {
    "global.quitConfirmation": "Vuoi davvero uscire?",
    "global.ok": "OK",
    "global.quit": "Esci",
    "global.cancel": "Annulla",
    "global.saveFile": "Salva file",
    "cocoa.menu.about": "Informazioni su",
    "cocoa.menu.services": "Servizi",
    "cocoa.menu.view": "Vista",
    "cocoa.menu.edit": "Modifica",
    "cocoa.menu.hide": "Nascondi",
    "cocoa.menu.hideOthers": "Nascondi altre",
    "cocoa.menu.showAll": "Mostra tutte",
    "cocoa.menu.quit": "Esci da",
    "cocoa.menu.fullscreen": "Entra in modalità a tutto schermo",
    "cocoa.menu.cut": "Taglia",
    "cocoa.menu.copy": "Copia",
    "cocoa.menu.paste": "Incolla",
    "cocoa.menu.selectAll": "Seleziona tutto",
    "windows.fileFilter.allFiles": "Tutti i file",
    "windows.fileFilter.otherFiles": "Altri tipi di file",
}

SMOKE_CHECK_JS ="window.__grabmediaReady === true && window.__grabmediaApi === true"
SMOKE_INFO_JS = """JSON.stringify({
  title: document.title,
  mode: (document.querySelector('#mode .on') || {dataset: {}}).dataset.mode || null,
  folder: (document.getElementById('folder-label') || {}).textContent || null,
  empty_state: !!document.getElementById('empty') && !document.getElementById('empty').classList.contains('hidden'),
  width: window.innerWidth, height: window.innerHeight
})"""


def _take_screenshot(path: str) -> None:
    """Fotografia dello schermo per la prova della finestra (solo con GRABMEDIA_SMOKE_SCREENSHOT)."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["screencapture", "-x", path], timeout=20, check=False)
        elif sys.platform == "win32":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                "$g=[System.Drawing.Graphics]::FromImage($i);$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                f"$i.Save('{path}')"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", script], timeout=30, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def _smoke_check(window, timeout: float = 90.0) -> int:
    """Aspetta che l'interfaccia sia pronta e collegata al programma, stampa un JSON."""
    import json

    report: dict = {"window": True, "app_ready": False}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if window.evaluate_js(SMOKE_CHECK_JS) is True:
                report["app_ready"] = True
                break
        except Exception as exc:  # noqa: BLE001 - pagina non ancora caricata
            report["last_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    if report["app_ready"]:
        report.pop("last_error", None)
        try:
            report.update(json.loads(window.evaluate_js(SMOKE_INFO_JS)))
        except Exception as exc:  # noqa: BLE001
            report["info_error"] = f"{type(exc).__name__}: {exc}"
        spec = os.environ.get("GRABMEDIA_SMOKE_DOWNLOAD", "")
        if spec:
            report["download"] = _smoke_download(window, spec)
        shot = os.environ.get("GRABMEDIA_SMOKE_SCREENSHOT")
        if shot:
            time.sleep(1.0)
            _take_screenshot(shot)
            report["screenshot"] = shot if Path(shot).is_file() else None
    print(json.dumps(report, ensure_ascii=False), flush=True)
    download_ok = report.get("download", {}).get("ok", True)
    return 0 if report["app_ready"] and download_ok else 1


def _smoke_download(window, spec: str, timeout: float = 300.0) -> dict:
    """Usa l'interfaccia come una persona (``audio|URL`` o ``video|URL``): sceglie
    il tipo, scrive il link, preme Scarica, aspetta la fine e controlla il file."""
    import json

    mode, _, url = spec.partition("|")
    mode = "video" if mode.strip() == "video" else "audio"
    result: dict = {"mode": mode, "url": url.strip(), "ok": False}
    window.evaluate_js(
        "(() => {"
        f" document.querySelector('#mode [data-mode=\"{mode}\"]').click();"
        " const u = document.getElementById('url');"
        f" u.value = {json.dumps(url.strip())};"
        " u.dispatchEvent(new Event('input'));"
        " document.getElementById('go').click();"
        " return true; })()"
    )
    deadline = time.monotonic() + timeout
    item: dict = {}
    while time.monotonic() < deadline:
        time.sleep(1.0)
        try:
            items = json.loads(window.evaluate_js("JSON.stringify(window.__grabmediaItems())") or "[]")
        except Exception:  # noqa: BLE001
            continue
        item = items[0] if items else {}
        if item.get("state") in ("done", "partial", "failed"):
            break
    result.update(state=item.get("state"), title=item.get("title"), error=item.get("error") or None,
                  files=item.get("files") or [])
    files = [Path(f) for f in result["files"]]
    result["file_exists"] = bool(files) and all(f.is_file() for f in files)
    result["in_folder"] = bool(files) and all(
        f.resolve().is_relative_to((video_dir() if mode == "video" else music_dir()).resolve()) for f in files)
    result["reveal_button"] = bool(window.evaluate_js(
        "!!document.querySelector('#list .row .icon-btn[aria-label^=\"Mostra\"]')"))
    result["ok"] = result["state"] == "done" and result["file_exists"] and result["in_folder"] \
        and result["reveal_button"]
    return result


def native_engine() -> str:
    """Motore della finestra nativa disponibile; solleva un errore se non c'è."""
    import webview  # noqa: F401

    if sys.platform == "win32":
        import webview.platforms.winforms as winforms

        if winforms.renderer != "edgechromium":
            raise RuntimeError(f"motore '{winforms.renderer}' non adatto: manca Microsoft Edge WebView2")
        return "edgechromium"
    if sys.platform == "darwin":
        import webview.platforms.cocoa  # noqa: F401

        return "cocoa"
    return "default"


def run_app(backend: Backend, *, smoke: bool = False) -> int:
    """Finestra nativa con l'interfaccia da app. Se il motore web manca: finestra di riserva."""
    import logging

    log = logging.getLogger("grabmedia.desktop")
    try:
        engine = native_engine()
        import webview
    except Exception as exc:  # noqa: BLE001
        log.warning("Finestra nativa non disponibile (%s): uso la finestra semplice + browser", exc)
        if smoke:
            print(f'{{"window": false, "error": "{type(exc).__name__}"}}', flush=True)
            return 1
        run_tk_gui(backend)
        return 0

    api = DesktopApi(backend.url)
    threading.Thread(target=lambda: _start_quietly(backend), daemon=True, name="avvio").start()
    dark = system_dark_mode()
    window = webview.create_window(
        APP_NAME, html=LOADING_HTML, js_api=api,
        width=820, height=600, min_size=(620, 460),
        background_color="#1C1C1E" if dark else "#F5F5F7", text_select=True,
    )
    api._window = window
    result = {"code": 0}

    def boot() -> None:
        if not backend.wait_ready(timeout=120):
            window.load_html(error_html(backend.error or "il server interno non risponde"))
            if smoke:
                print('{"window": true, "app_ready": false, "error": "backend"}', flush=True)
                result["code"] = 1
                window.destroy()
            return
        window.load_url(backend.url + "/app")
        if smoke:
            result["code"] = _smoke_check(window)
            window.destroy()

    try:
        webview.start(boot, private_mode=True, localization=LOCALIZATION_IT,
                      gui="edgechromium" if engine == "edgechromium" else None)
    except Exception as exc:  # noqa: BLE001
        log.exception("La finestra nativa si è chiusa con un errore")
        result["code"] = 1
        if smoke:
            print(f'{{"window": false, "error": "{type(exc).__name__}"}}', flush=True)
    finally:
        backend.stop()
    return result["code"]


def _start_quietly(backend: Backend) -> None:
    try:
        backend.start()
    except Exception:  # noqa: BLE001 - già registrato in backend.error e mostrato nella finestra
        pass


# --------------------------------------------------------- finestra di riserva
def run_tk_gui(backend: Backend) -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("380x230")
    root.resizable(False, False)
    status = tk.StringVar(value="Avvio in corso…")
    opened = {"done": False}

    ttk.Label(root, text=APP_NAME, font=("", 18, "bold")).pack(pady=(18, 2))
    ttk.Label(root, textvariable=status, wraplength=340, justify="center").pack(pady=(0, 12))
    open_btn = ttk.Button(root, text="Apri GrabMedia", command=lambda: webbrowser.open(backend.url), state="disabled")
    open_btn.pack(fill="x", padx=40, pady=3)
    ttk.Button(root, text="Apri la cartella della musica", command=lambda: open_folder(music_dir())).pack(fill="x", padx=40, pady=3)

    def quit_app() -> None:
        status.set("Chiusura…")
        root.update_idletasks()
        backend.stop()
        root.destroy()

    ttk.Button(root, text="Esci", command=quit_app).pack(fill="x", padx=40, pady=(3, 12))
    root.protocol("WM_DELETE_WINDOW", quit_app)

    def start_backend() -> None:
        try:
            backend.start()
        except Exception:  # noqa: BLE001 - già registrato in backend.error
            pass

    threading.Thread(target=start_backend, daemon=True, name="avvio").start()

    def poll() -> None:
        if backend.error:
            status.set(f"Avvio non riuscito: {backend.error}\nDettagli in {data_dir() / 'grabmedia.log'}")
            return
        if backend.ready():
            status.set(f"In esecuzione su {backend.url}\nLascia aperta questa finestra mentre scarichi.")
            open_btn.config(state="normal")
            if not opened["done"]:
                opened["done"] = True
                if os.environ.get("GRABMEDIA_NO_BROWSER", "").strip() not in ("1", "true", "yes"):
                    webbrowser.open(backend.url)
            return
        root.after(300, poll)

    root.after(300, poll)
    root.mainloop()


def _raise_keyboard_interrupt(signum, frame) -> None:  # noqa: ARG001
    raise KeyboardInterrupt


def run_headless(backend: Backend) -> int:
    import signal

    try:  # "termina" (SIGTERM) ferma server e worker come Ctrl+C
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except (ValueError, OSError):
        pass
    backend.start()
    if not backend.wait_ready():
        print(f"Avvio non riuscito: {backend.error or 'tempo scaduto'}", flush=True)
        backend.stop()
        return 1
    print(f"GrabMedia in esecuzione su {backend.url} (Ctrl+C per uscire)", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        backend.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = mode_from_argv(argv)
    if mode == "ytdlp":
        run_ytdlp(argv[2:])
        return 0
    if mode == "worker":
        run_worker(argv)
        return 0
    if mode == "selfcheck":
        return run_self_check()
    ensure_std_streams()
    port = None
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            port = None
    backend = Backend(port)
    if mode == "nogui":
        return run_headless(backend)
    if mode == "tk":
        run_tk_gui(backend)
        return 0
    return run_app(backend, smoke=mode == "guismoke")


def hard_exit(code: int) -> None:
    """Chiude il processo subito dopo la pulizia. I thread del Redis in memoria
    (uno per connessione, non daemon) terrebbero vivo il programma in background
    anche dopo "Esci": visto davvero con l'eseguibile impacchettato."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


if __name__ == "__main__":
    hard_exit(main())  # yt-dlp esce da sé con SystemExit e il suo codice: non passa di qui
