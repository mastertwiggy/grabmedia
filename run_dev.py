"""Avvio locale senza Docker: Redis in memoria (fakeredis), API FastAPI e worker RQ.

Uso:
    pip install -r requirements-dev.txt
    python run_dev.py

Variabili opzionali: API_PORT (default 8000), REDIS_PORT (default 6379), DOWNLOAD_DIR.
Se su REDIS_PORT c'è già un Redis vero viene usato quello.
"""
import glob
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API_PORT = int(os.getenv("API_PORT", "8000"))
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    local = os.getenv("LOCALAPPDATA", "")
    for path in glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "ffmpeg-*", "bin")):
        os.environ["PATH"] = path + os.pathsep + os.environ["PATH"]
        print(f"[dev] ffmpeg trovato in {path}")
        return
    print("[dev] ATTENZIONE: ffmpeg non trovato nel PATH (mp3 e merge video non funzioneranno)")


def ensure_js_runtime() -> None:
    """Come per ffmpeg: dopo `winget install DenoLand.Deno` il PATH aggiornato vale
    solo per i programmi avviati in seguito, quindi deno si cerca anche nella
    cartella di winget. Senza runtime il provider YouTube di Provider audio resta
    disabilitato (vedi README)."""
    if shutil.which("deno") or shutil.which("node") or os.getenv("YTDLP_JS_RUNTIME"):
        return
    local = os.getenv("LOCALAPPDATA", "")
    patterns = [
        os.path.join(local, "Microsoft", "WinGet", "Packages", "DenoLand.Deno*", "deno.exe"),
        os.path.join(local, "Microsoft", "WinGet", "Links", "deno.exe"),
        os.path.join(os.path.expanduser("~"), ".deno", "bin", "deno*"),
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            os.environ["PATH"] = os.path.dirname(path) + os.pathsep + os.environ["PATH"]
            print(f"[dev] deno trovato in {path}")
            return
    print("[dev] ATTENZIONE: nessun runtime JavaScript (deno/node): YouTube e Spotify in Provider audio non funzioneranno")


def start_fake_redis() -> None:
    from fakeredis import TcpFakeServer

    server = TcpFakeServer(("127.0.0.1", REDIS_PORT), server_type="redis", server_version=(7, 2))
    threading.Thread(target=server.serve_forever, daemon=True, name="fakeredis").start()
    for _ in range(50):
        if port_open(REDIS_PORT):
            break
        time.sleep(0.1)
    print(f"[dev] Redis in memoria (fakeredis) su 127.0.0.1:{REDIS_PORT}")


def main() -> None:
    ensure_ffmpeg()
    ensure_js_runtime()
    env = os.environ.copy()
    env["REDIS_HOST"] = "127.0.0.1"
    env["REDIS_PORT"] = str(REDIS_PORT)
    env.setdefault("DOWNLOAD_DIR", str(ROOT / "downloads"))

    use_fake = not port_open(REDIS_PORT)
    if use_fake:
        start_fake_redis()
    else:
        print(f"[dev] Uso il Redis già in ascolto su 127.0.0.1:{REDIS_PORT}")

    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=ROOT,
        env=env,
    )

    if use_fake:
        # fakeredis non supporta CLIENT LIST (usato da RQ solo per mostrare l'IP del worker)
        worker_code = (
            "import redis; redis.Redis.client_list = lambda self, *a, **k: []; "
            "from app.worker import main; main()"
        )
        worker_cmd = [sys.executable, "-c", worker_code]
    else:
        worker_cmd = [sys.executable, "-m", "app.worker"]
    worker = subprocess.Popen(worker_cmd, cwd=ROOT, env=env)

    print(f"\n[dev] GrabMedia pronto: http://127.0.0.1:{API_PORT}  (Ctrl+C per fermare)\n")
    try:
        while True:
            if api.poll() is not None:
                print("[dev] L'API si è fermata, chiudo.")
                break
            if worker.poll() is not None:
                print("[dev] Il worker si è fermato, chiudo.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[dev] Arresto...")
    finally:
        for proc in (worker, api):
            if proc.poll() is None:
                proc.terminate()
        for proc in (worker, api):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
