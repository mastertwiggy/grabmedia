"""Costruisce il programma desktop di GrabMedia con PyInstaller.

Uso (dalla cartella del progetto, con le dipendenze di requirements-desktop.txt):

    python desktop/build.py            # prepara ffmpeg e deno, impacchetta, crea lo zip
    python desktop/build.py --no-zip   # solo la cartella/app, senza zip

Risultato in ``desktop/dist``:

* Windows: ``GrabMedia/GrabMedia.exe`` (cartella intera) e ``GrabMedia-windows-x64.zip``;
* macOS:   ``GrabMedia.app`` e ``GrabMedia-macos-<arm64|x64>.zip``.

Il pacchetto va costruito sul sistema a cui è destinato: PyInstaller non
compila per un altro sistema operativo (per il Mac c'è il workflow
``.github/workflows/build-desktop.yml``).

ffmpeg arriva dal pacchetto ``imageio-ffmpeg``; deno è quello installato sulla
macchina (``GRABMEDIA_DENO``, poi il PATH, poi la cartella di winget).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
ROOT = DESKTOP.parent
VENDOR_BIN = DESKTOP / "vendor" / "bin"
BUILD_DIR = DESKTOP / "build"
DIST_DIR = DESKTOP / "dist"
APP_NAME = "GrabMedia"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

# Pacchetti del progetto: i provider vengono caricati per nome a runtime (registry),
# quindi PyInstaller non li vede dagli import. Si elencano i file uno per uno:
# --collect-submodules non basta, perché cerca il pacchetto tra quelli installati.
PROJECT_PACKAGES = ("sources", "core", "app")
# Pacchetti installati i cui moduli vengono caricati per nome a runtime.
COLLECT_SUBMODULES = ("rq", "uvicorn", "fakeredis")
COLLECT_DATA = ("fakeredis",)
COLLECT_ALL = ("yt_dlp_ejs",)  # script JS per le firme di YouTube
EXCLUDE = ("pytest", "vcr", "httpx", "IPython", "matplotlib", "numpy",
           # motori della finestra che non usiamo (pywebview li importa solo se scelti)
           "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "cefpython3")
ASSETS = DESKTOP / "assets"


def window_modules() -> list[str]:
    """Moduli del motore della finestra nativa per il sistema su cui si costruisce."""
    if sys.platform == "win32":
        return ["webview.platforms.winforms", "webview.platforms.edgechromium"]
    if sys.platform == "darwin":
        return ["webview.platforms.cocoa"]
    return []


def icon_path() -> Path:
    """Icona: griglia Apple su macOS, a tutta superficie su Windows (PyInstaller la converte)."""
    return ASSETS / ("icon-mac.png" if sys.platform == "darwin" else "icon-win.png")


class BuildError(RuntimeError):
    pass


def arch_label() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x64"


def archive_name() -> str:
    system = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
    return f"{APP_NAME}-{system}-{arch_label()}.zip"


def find_deno() -> Path:
    """deno da includere: ``GRABMEDIA_DENO``, poi il PATH, poi la cartella di winget."""
    candidates: list[Path] = []
    if os.environ.get("GRABMEDIA_DENO"):
        candidates.append(Path(os.environ["GRABMEDIA_DENO"]))
    found = shutil.which("deno")
    if found:
        candidates.append(Path(found))
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        packages = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            candidates.extend(sorted(packages.glob("DenoLand.Deno_*/deno.exe")))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise BuildError(
        "deno non trovato. Installalo (Windows: winget install DenoLand.Deno; macOS: brew install deno) "
        "oppure indica il file con la variabile GRABMEDIA_DENO."
    )


def find_ffmpeg() -> Path:
    try:
        import imageio_ffmpeg
    except ImportError:
        raise BuildError("Manca imageio-ffmpeg: pip install -r requirements-desktop.txt") from None
    return Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()


def vendor_tools() -> dict[str, Path]:
    """Copia ffmpeg e deno in ``desktop/vendor/bin`` con i nomi che il programma si aspetta."""
    VENDOR_BIN.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for name, source in (("ffmpeg", find_ffmpeg()), ("deno", find_deno())):
        target = VENDOR_BIN / f"{name}{EXE_SUFFIX}"
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
        target.chmod(0o755)
        result[name] = target
    return result


def project_modules() -> list[str]:
    """Tutti i moduli dei pacchetti del progetto (``sources.youtube``, ``core.api``…)."""
    names: list[str] = []
    for package in PROJECT_PACKAGES:
        for path in sorted((ROOT / package).glob("*.py")):
            names.append(package if path.stem == "__init__" else f"{package}.{path.stem}")
    return names


def pyinstaller_args(tools: dict[str, Path]) -> list[str]:
    sep = os.pathsep  # separatore origine/destinazione di --add-data
    args = [
        str(DESKTOP / "grabmedia_desktop.py"),
        "--name", APP_NAME,
        "--noconfirm", "--clean", "--windowed", "--onedir",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'frontend' / 'index.html'}{sep}frontend",
        "--add-data", f"{ROOT / 'frontend' / 'app.html'}{sep}frontend",
        "--icon", str(icon_path()),
    ]
    for module in window_modules():
        args += ["--hidden-import", module]
    for path in tools.values():
        args += ["--add-binary", f"{path}{sep}bin"]
    for module in project_modules():
        args += ["--hidden-import", module]
    for package in COLLECT_SUBMODULES:
        args += ["--collect-submodules", package]
    for package in COLLECT_DATA:
        args += ["--collect-data", package]
    for package in COLLECT_ALL:
        args += ["--collect-all", package]
    for module in EXCLUDE:
        args += ["--exclude-module", module]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "local.grabmedia.desktop"]
    return args


def built_path() -> Path:
    return DIST_DIR / (f"{APP_NAME}.app" if sys.platform == "darwin" else APP_NAME)


def built_executable() -> Path:
    if sys.platform == "darwin":
        return built_path() / "Contents" / "MacOS" / APP_NAME
    return built_path() / f"{APP_NAME}{EXE_SUFFIX}"


def make_archive() -> Path:
    target = DIST_DIR / archive_name()
    target.unlink(missing_ok=True)
    source = built_path()
    if sys.platform == "darwin":
        # ditto conserva collegamenti simbolici e permessi della .app (zipfile no)
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(source), str(target)], check=True)
    else:
        shutil.make_archive(str(target.with_suffix("")), "zip", root_dir=DIST_DIR, base_dir=source.name)
    return target


def build(*, make_zip: bool = True) -> Path:
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        raise BuildError("Manca PyInstaller: pip install -r requirements-desktop.txt") from None
    tools = vendor_tools()
    shutil.rmtree(built_path(), ignore_errors=True)
    pyinstaller.run(pyinstaller_args(tools))
    exe = built_executable()
    if not exe.is_file():
        raise BuildError(f"PyInstaller non ha prodotto {exe}")
    return make_archive() if make_zip else built_path()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Costruisce il programma desktop di GrabMedia")
    parser.add_argument("--no-zip", action="store_true", help="non creare lo zip finale")
    args = parser.parse_args(argv)
    try:
        result = build(make_zip=not args.no_zip)
    except BuildError as exc:
        print(f"ERRORE: {exc}", file=sys.stderr)
        return 1
    print(f"Fatto: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
