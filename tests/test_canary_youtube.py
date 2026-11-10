"""Canary con RETE REALE su YouTube: gira solo con GRABMEDIA_CANARY=1 (CI del
job di bump, dove la variabile è esplicita e un salto fa fallire il job; in
locale resta saltabile).

Cosa verifica, in ordine di importanza:

1. **Gate primario: runtime rotto.** Con ``verbose`` attivo, qualunque riga di
   fallback sulla risoluzione della signature (``SIGNATURE_FALLBACK_RE``:
   ``nsig``, ``Signature solving``, ``unable to solve``) è un fallimento, anche
   se i formati sembrano a posto.
2. **Runtime JS rilevato.** ``core.jsruntime.detect()`` deve trovare un runtime,
   la riga di rilevamento di yt-dlp (``[debug] JS runtimes: ...``) deve esserci e
   non dire ``none``, e non deve comparire il WARNING "No supported JavaScript
   runtime".
3. **Formato ammesso.** ``-f bestaudio`` deve scegliere 251 (opus) o 140 (m4a):
   qualunque altro id significa che il catalogo dei formati è cambiato o degradato.
4. **Pavimento di bitrate.** La mediana sui due video deve superare 100 kbps.
   È SOLO un pavimento contro il degrado (es. ripiego su 249/250 a 50-65 kbps),
   NON una misura di qualità: opus 251 sta intorno a 127-129 kbps con o senza
   runtime, quindi il bitrate non distingue i due casi. Il gate vero è il punto 1.

La logica di valutazione è in ``evaluate()`` (pura) ed è coperta offline da
``tests/test_canary_logic.py``. Non usa il guard di conftest che blocca la
rete: qui la rete serve."""

from __future__ import annotations

import os
import re
import socket
import statistics
from dataclasses import dataclass, field

import pytest

# Due caricamenti UFFICIALI e stabili (canale dell'artista/etichetta, online da
# anni, mai regionali). Riverificarli: a ogni bump del limite superiore del
# range di yt-dlp (oggi <2027) e comunque ogni trimestre; se uno sparisce o
# diventa privato, sostituirlo con un altro caricamento ufficiale e annotarlo qui.
# Ultima verifica: 2026-11-06.
CANARY_VIDEOS: tuple[str, ...] = (
    "dQw4w9WgXcQ",  # Rick Astley - Never Gonna Give You Up (canale ufficiale Rick Astley)
    "9bZkp7q19f0",  # PSY - GANGNAM STYLE (canale ufficiale officialpsy)
)
# Solo un PAVIMENTO contro il degrado, non una misura di qualità: il gate
# primario resta SIGNATURE_FALLBACK_RE (vedi docstring del modulo).
MIN_MEDIAN_ABR_KBPS = 100.0
# Formati audio che -f bestaudio può legittimamente scegliere: opus 251 o m4a 140.
ALLOWED_FORMAT_IDS: frozenset[str] = frozenset({"251", "140"})
RUNTIME_WARNING_RE = re.compile(r"No supported JavaScript runtime", re.I)
# Riga di rilevamento nell'header verbose di yt-dlp: "[debug] JS runtimes: deno-2.5.6" / "none"
RUNTIME_LINE_RE = re.compile(r"JS runtimes:\s*(.*)$", re.I)
# Righe di fallback sulla risoluzione della signature (runtime presente ma rotto / solver che cede)
SIGNATURE_FALLBACK_RE = re.compile(
    r"unable to solve|nsig[^\n]*(fail|unable|fallback|falling back|skipp)|"
    r"(fail|unable|fallback|falling back)[^\n]*nsig|Signature solving[^\n]*(fail|unable|fallback|falling back)|"
    r"(fail|unable|falling back)[^\n]*Signature solving",
    re.I,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("GRABMEDIA_CANARY", "").strip() not in ("1", "true", "yes"),
    reason="canary con rete reale: abilitalo con GRABMEDIA_CANARY=1",
)


@pytest.fixture(autouse=True)
def allow_network(monkeypatch):
    """Ripristina i socket veri sopra il guard di conftest."""
    import importlib

    real_socket = importlib.reload(socket)
    monkeypatch.setattr(socket.socket, "connect", real_socket.socket.connect)
    monkeypatch.setattr(socket, "create_connection", real_socket.create_connection)


class CaptureLogger:
    """Logger per yt-dlp che conserva TUTTE le righe (debug compreso, = --verbose)."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg: str) -> None:
        self.lines.append(msg)

    def info(self, msg: str) -> None:
        self.lines.append(msg)

    def warning(self, msg: str) -> None:
        self.lines.append("WARNING: " + msg)

    def error(self, msg: str) -> None:
        self.lines.append("ERROR: " + msg)


@dataclass(slots=True)
class CanaryVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    median_abr: float | None = None
    runtime_line: str | None = None


def bitrate_kbps(info: dict) -> float | None:
    for key in ("abr", "tbr"):
        value = info.get(key)
        if value:
            return float(value)
    return None


def detected_runtime_line(log_lines: list[str]) -> str | None:
    """Valore della riga ``JS runtimes:`` di yt-dlp (``None`` se la riga manca)."""
    for line in log_lines:
        m = RUNTIME_LINE_RE.search(line)
        if m:
            return m.group(1).strip()
    return None


def evaluate(
    log_lines: list[str],
    runtime_detected: bool,
    abr_values: list[float | None],
    format_ids: list[str | None],
) -> CanaryVerdict:
    """Logica pura del canary: righe di log (verbose), runtime rilevato dal core,
    bitrate e id del formato scelto per ciascun video. Tutte le ragioni di
    fallimento vengono raccolte, non solo la prima."""
    reasons: list[str] = []

    # 1. gate primario: fallback sulla risoluzione della signature
    fallbacks = [ln for ln in log_lines if SIGNATURE_FALLBACK_RE.search(ln)]
    if fallbacks:
        reasons.append("fallback sulla risoluzione della signature (runtime rotto): " + fallbacks[0][:200])

    # 2. runtime JS rilevato: dal core, dalla riga di yt-dlp, e nessun warning
    if not runtime_detected:
        reasons.append("nessun runtime JavaScript rilevato (deno/node): installa deno sul runner del canary")
    runtime_line = detected_runtime_line(log_lines)
    if runtime_line is None:
        reasons.append("riga di rilevamento 'JS runtimes:' assente dall'output verbose di yt-dlp")
    elif runtime_line.lower().startswith("none") or not runtime_line:
        reasons.append(f"yt-dlp non rileva nessun runtime JS (JS runtimes: {runtime_line or 'vuoto'})")
    runtime_warnings = [ln for ln in log_lines if RUNTIME_WARNING_RE.search(ln)]
    if runtime_warnings:
        reasons.append("yt-dlp segnala il runtime JS assente: " + runtime_warnings[0][:200])

    # 3. id del formato scelto: solo 251 o 140
    unexpected = [str(fid) for fid in format_ids if str(fid) not in ALLOWED_FORMAT_IDS]
    if unexpected:
        reasons.append(
            f"formato inatteso scelto da -f bestaudio: {', '.join(unexpected)} "
            f"(ammessi: {', '.join(sorted(ALLOWED_FORMAT_IDS))})"
        )

    # 4. pavimento di bitrate (solo contro il degrado, non misura di qualità)
    known = [v for v in abr_values if v is not None]
    median = statistics.median(known) if known else None
    if len(known) < len(abr_values):
        reasons.append(f"bitrate non riportato per {len(abr_values) - len(known)} video su {len(abr_values)}")
    if median is None:
        reasons.append("nessun bitrate misurabile")
    elif median <= MIN_MEDIAN_ABR_KBPS:
        reasons.append(f"mediana del bitrate di -f bestaudio = {median:.1f} kbps (<= pavimento {MIN_MEDIAN_ABR_KBPS:.0f})")
    return CanaryVerdict(ok=not reasons, reasons=reasons, median_abr=median, runtime_line=runtime_line)


def test_youtube_bestaudio_with_js_runtime():
    import yt_dlp

    from core import jsruntime
    from sources import _ytdlp

    runtime = jsruntime.detect()
    logger = CaptureLogger()
    opts = _ytdlp.base_opts({}, noplaylist=True)
    opts.update({"format": "bestaudio/best", "skip_download": True, "logger": logger,
                 "verbose": True, "quiet": False})
    if runtime is not None:
        opts["js_runtimes"] = {runtime.name: ({"path": runtime.path} if runtime.path else {})}
    abr_values: list[float | None] = []
    format_ids: list[str | None] = []
    chosen_desc: list[str] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for video_id in CANARY_VIDEOS:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            chosen = info.get("requested_formats", [info])[0] if info.get("requested_formats") else info
            abr_values.append(bitrate_kbps(chosen))
            format_ids.append(chosen.get("format_id"))
            chosen_desc.append(f"{video_id}: {chosen.get('format_id')} {chosen.get('acodec')} {bitrate_kbps(chosen)} kbps")

    verdict = evaluate(logger.lines, runtime is not None, abr_values, format_ids)
    print(f"runtime JS (core): {runtime.as_env() if runtime else 'NESSUNO'} | yt-dlp JS runtimes: {verdict.runtime_line} | "
          + " | ".join(chosen_desc) + f" | mediana: {verdict.median_abr} | righe di log: {len(logger.lines)}")
    assert verdict.ok, "canary YouTube fallito:\n- " + "\n- ".join(verdict.reasons)
