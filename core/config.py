"""Caricamento di ``config.toml`` e di ``.env``.

``config.toml`` governa il core: cartella di output, template di naming,
concorrenza (default e per provider), sovrascrittura, priorità dei provider e
qualità. Il file ``.env`` contiene solo credenziali, viene caricato in
``os.environ`` senza che il core sappia a quale provider servano.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("grabmedia.config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(ValueError):
    """Config non valida, con indicazione di cosa correggere."""


@dataclass(slots=True)
class Config:
    out_dir: Path = PROJECT_ROOT / "downloads" / "music"
    naming: str = "{artist}/{album}/{track_no:02d} - {title}"
    overwrite: bool = False
    priority: list[str] = field(default_factory=list)
    concurrency_default: int = 2
    concurrency: dict[str, int] = field(default_factory=dict)
    quality_default: str = "best"
    quality: dict[str, str] = field(default_factory=dict)
    state_db: Path = PROJECT_ROOT / "downloads" / "state.sqlite3"
    max_attempts: int = 4
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    circuit_threshold: int = 5
    circuit_cooldown: float = 60.0
    options: dict[str, dict] = field(default_factory=dict)
    source_path: Path | None = None

    def options_for(self, provider: str) -> dict:
        return dict(self.options.get(provider, {}))

    def concurrency_for(self, provider: str) -> int:
        return max(1, int(self.concurrency.get(provider, self.concurrency_default)))

    def quality_for(self, provider: str) -> str:
        return str(self.quality.get(provider, self.quality_default))


def _as_path(value: object, base: Path, what: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{what}' deve essere una stringa non vuota in config.toml")
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else (base / path).resolve()


def _as_int(value: object, what: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"'{what}' deve essere un intero >= {minimum} in config.toml")
    return value


def from_dict(data: dict, base: Path = PROJECT_ROOT, source: Path | None = None) -> Config:
    cfg = Config(source_path=source)
    general = data.get("general", {})
    if not isinstance(general, dict):
        raise ConfigError("[general] deve essere una tabella in config.toml")
    if "out_dir" in general:
        cfg.out_dir = _as_path(general["out_dir"], base, "general.out_dir")
    if "naming" in general:
        if not isinstance(general["naming"], str) or "{title}" not in general["naming"]:
            raise ConfigError("'general.naming' deve essere una stringa che contiene almeno {title}")
        cfg.naming = general["naming"]
    if "overwrite" in general:
        if not isinstance(general["overwrite"], bool):
            raise ConfigError("'general.overwrite' deve essere true o false")
        cfg.overwrite = general["overwrite"]
    if "state_db" in general:
        cfg.state_db = _as_path(general["state_db"], base, "general.state_db")

    priority = data.get("priority", {})
    if isinstance(priority, dict):
        order = priority.get("order", [])
    else:
        order = priority
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise ConfigError("'priority.order' deve essere una lista di nomi di provider")
    cfg.priority = [x.strip() for x in order if x.strip()]

    concurrency = data.get("concurrency", {})
    if not isinstance(concurrency, dict):
        raise ConfigError("[concurrency] deve essere una tabella: default = N, <provider> = N")
    for key, value in concurrency.items():
        n = _as_int(value, f"concurrency.{key}")
        if key == "default":
            cfg.concurrency_default = n
        else:
            cfg.concurrency[key] = n

    quality = data.get("quality", {})
    if not isinstance(quality, dict):
        raise ConfigError("[quality] deve essere una tabella: default = \"...\", <provider> = \"...\"")
    for key, value in quality.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"'quality.{key}' deve essere una stringa non vuota")
        if key == "default":
            cfg.quality_default = value.strip()
        else:
            cfg.quality[key] = value.strip()

    retry = data.get("retry", {})
    if not isinstance(retry, dict):
        raise ConfigError("[retry] deve essere una tabella")
    if "max_attempts" in retry:
        cfg.max_attempts = _as_int(retry["max_attempts"], "retry.max_attempts")
    for key in ("backoff_base", "backoff_cap", "circuit_cooldown"):
        if key in retry:
            value = retry[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(f"'retry.{key}' deve essere un numero >= 0")
            setattr(cfg, key, float(value))
    if "circuit_threshold" in retry:
        cfg.circuit_threshold = _as_int(retry["circuit_threshold"], "retry.circuit_threshold")

    options = data.get("options", {})
    if not isinstance(options, dict) or not all(isinstance(v, dict) for v in options.values()):
        raise ConfigError("[options] deve contenere solo tabelle [options.<provider>] con chiave = valore")
    for name, table in options.items():
        resolved: dict = {}
        for key, value in table.items():
            if isinstance(value, str) and key.endswith(("file", "_location", "_path", "_dir")) and value.strip():
                resolved[key] = str(_as_path(value, base, f"options.{name}.{key}"))
            else:
                resolved[key] = value
        cfg.options[name] = resolved
    return cfg


def load(path: Path | str | None = None) -> Config:
    """Legge ``config.toml`` (o il file indicato). Se manca, usa i default."""
    # GRABMEDIA_CONFIG: percorso alternativo (es. il programma desktop, che tiene
    # configurazione e dati nella cartella dell'utente e non in quella del codice).
    path = path or os.environ.get("GRABMEDIA_CONFIG") or None
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        if path:
            raise ConfigError(f"File di configurazione non trovato: {cfg_path}")
        log.info("config.toml assente, uso i valori di default")
        return Config()
    try:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{cfg_path} non è TOML valido: {exc}") from None
    return from_dict(data, base=cfg_path.parent, source=cfg_path)


def load_env(path: Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    """Carica ``KEY=VALUE`` da ``.env`` in ``os.environ``.

    Righe vuote e commenti sono ignorati; i valori possono essere tra virgolette.
    Le variabili già presenti nell'ambiente vincono, salvo ``override=True``.
    """
    env_path = Path(path) if path else DEFAULT_ENV_PATH
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for lineno, raw in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            log.warning("%s:%d ignorata (manca '=')", env_path, lineno)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded
