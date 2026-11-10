"""Programma desktop di GrabMedia (Windows e macOS).

Un solo eseguibile che fa tutto in locale, senza installare niente:

* finestra piccola (tkinter) con "Apri GrabMedia", "Apri la cartella della musica", "Esci";
* server web interno su 127.0.0.1 (porta libera), lo stesso sito del progetto;
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
default = "best"

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
    try:
        from app.main import FRONTEND_INDEX

        report["frontend"] = FRONTEND_INDEX.is_file()
    except Exception as exc:  # noqa: BLE001
        report["frontend"] = False
        report["problems"].append(f"sito non importabile: {exc}")
    if report["frontend"] is False and not any("sito" in p for p in report["problems"]):
        report["problems"].append("pagina del sito mancante")
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
        try:
            env = prepare_environment(self.redis_port)
            from fakeredis import TcpFakeServer

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
            self._server = uvicorn.Server(config)
            self._thread = threading.Thread(target=self._server.run, daemon=True, name="uvicorn")
            self._thread.start()
        except Exception as exc:  # noqa: BLE001 - l'errore va mostrato nella finestra, non perso
            self.error = f"{type(exc).__name__}: {exc}"
            raise

    def ready(self) -> bool:
        return self.error is None and port_open(self.port)

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


# ------------------------------------------------------------------------ GUI
def run_gui(backend: Backend) -> None:
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
    run_gui(backend)
    return 0


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
