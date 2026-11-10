"""Rilevamento del runtime JavaScript richiesto da yt-dlp per i siti che lo pretendono.

Ordine: override esplicito ``YTDLP_JS_RUNTIME`` (``deno``, ``node`` oppure
``nome:/percorso``), poi ``deno`` nel PATH, poi ``node``. Il risultato viene
esportato in ``YTDLP_JS_RUNTIME`` così i provider basati su yt-dlp lo passano
come ``js_runtimes`` esplicito. Se manca, la CLI esce con le istruzioni di
installazione, salvo ``--allow-no-js-runtime`` (o ``GRABMEDIA_ALLOW_NO_JS_RUNTIME=1``).
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger("grabmedia.jsruntime")

ENV_VAR = "YTDLP_JS_RUNTIME"
ALLOW_ENV_VAR = "GRABMEDIA_ALLOW_NO_JS_RUNTIME"
CANDIDATES = ("deno", "node")
INSTALL_INSTRUCTIONS = (
    "brew install deno",
    "winget install DenoLand.Deno",
    "curl -fsSL https://deno.land/install.sh | sh",
)


@dataclass(frozen=True, slots=True)
class JsRuntime:
    name: str
    path: str | None = None

    def as_env(self) -> str:
        return f"{self.name}:{self.path}" if self.path else self.name


class JsRuntimeMissing(RuntimeError):
    """Nessun runtime JavaScript trovato: il messaggio contiene le istruzioni."""

    def __init__(self) -> None:
        lines = ["Nessun runtime JavaScript trovato (yt-dlp lo richiede per i siti che lo pretendono). Installa deno:"]
        lines += [f"  {cmd}" for cmd in INSTALL_INSTRUCTIONS]
        lines.append(f"oppure indica il runtime con {ENV_VAR}=deno:/percorso (o node), "
                     "oppure prosegui senza con --allow-no-js-runtime.")
        super().__init__("\n".join(lines))


Which = Callable[[str], str | None] | None


def _which(which: Which) -> Callable[[str], str | None]:
    # risolto a ogni chiamata: così un monkeypatch di shutil.which viene rispettato
    return which if which is not None else shutil.which


def parse_override(value: str | None, which: Which = None) -> JsRuntime | None:
    """``"deno"`` -> cerca nel PATH; ``"node:/usr/bin/node"`` -> percorso esplicito;
    un percorso assoluto -> nome dedotto dal file."""
    which = _which(which)
    if not value or not value.strip():
        return None
    value = value.strip()
    if ":" in value and not (len(value) > 1 and value[1] == ":" and value[0].isalpha()):  # nome:percorso (non C:\...)
        name, path = value.split(":", 1)
        return JsRuntime(name.strip().lower(), path.strip() or None)
    if os.sep in value or "/" in value:
        name = os.path.basename(value).lower().removesuffix(".exe")
        return JsRuntime(name, value)
    name = value.lower()
    return JsRuntime(name, which(name))


def _explicit_path(value: str | None) -> bool:
    """``nome:percorso`` o un percorso assoluto: l'utente ha indicato il binario."""
    if not value:
        return False
    value = value.strip()
    return (":" in value and not (len(value) > 1 and value[1] == ":" and value[0].isalpha())) \
        or os.sep in value or "/" in value


def _usable(runtime: JsRuntime | None, explicit: bool) -> bool:
    """Nome risolto dal PATH: basta che ``which`` l'abbia trovato. Percorso
    esplicito: il file deve esistere davvero."""
    if runtime is None or not runtime.path:
        return False
    return os.path.isfile(runtime.path) if explicit else True


def detect(env: Mapping[str, str] | None = None, which: Which = None) -> JsRuntime | None:
    env = os.environ if env is None else env
    which = _which(which)
    raw = env.get(ENV_VAR)
    override = parse_override(raw, which)
    if override is not None:
        if _usable(override, _explicit_path(raw)):
            return override
        # binario inesistente: avvisa e prosegui con il rilevamento automatico
        log.warning("%s=%r indica un runtime non trovato: ignoro e rilevo automaticamente",
                    ENV_VAR, env.get(ENV_VAR))
    for name in CANDIDATES:
        path = which(name)
        if path:
            return JsRuntime(name, path)
    return None


def ensure(*, allow_missing: bool = False, env: Mapping[str, str] | None = None,
           which: Which = None) -> JsRuntime | None:
    """Rileva ed esporta il runtime in ``YTDLP_JS_RUNTIME``; solleva
    ``JsRuntimeMissing`` se non c'è e non è consentito proseguire senza."""
    runtime = detect(env, which)
    if runtime is None:
        if allow_missing or (env or os.environ).get(ALLOW_ENV_VAR, "").strip() in ("1", "true", "yes"):
            return None
        raise JsRuntimeMissing()
    os.environ[ENV_VAR] = runtime.as_env()
    return runtime
