"""Scoperta e caricamento lazy dei provider in ``sources/``.

Convenzione: ``sources/<nome>.py`` espone una o più sottoclassi concrete di
``AudioSource``. Ogni provider è importato solo quando serve; un modulo che
fallisce l'import (dipendenza mancante, bug) finisce in ``UNAVAILABLE`` con il
motivo e non impedisce agli altri di funzionare.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from types import ModuleType

import sources
from sources.base import AudioSource, MetadataSource, ProviderError, ProviderUnavailable

log = logging.getLogger("grabmedia.registry")

REGISTRY: dict[str, AudioSource] = {}
"""Provider istanziati con successo, per nome. Popolato a runtime."""

UNAVAILABLE: dict[str, str] = {}
"""Provider che non si sono caricati: ``nome -> motivo``."""

OPTIONS: dict[str, dict] = {}
"""Opzioni per provider (``[options.<nome>]`` di config.toml), passate al
costruttore al momento del caricamento. Impostare prima di ``load``."""


PRIORITY: list[str] = []
"""Ordine di priorità dei provider (``priority.order`` di config.toml)."""

DISABLED: dict[str, str] = {}
"""Provider disabilitati dall'applicazione: ``nome -> motivo`` (es. manca un
runtime richiesto). Non vengono caricati e risultano ``UNAVAILABLE``."""


def configure(options: dict[str, dict] | None, priority: list[str] | None = None,
              disabled: dict[str, str] | None = None) -> None:
    OPTIONS.clear()
    OPTIONS.update({k: dict(v) for k, v in (options or {}).items()})
    PRIORITY[:] = list(priority or [])
    DISABLED.clear()
    DISABLED.update(dict(disabled or {}))


def disable(name: str, reason: str) -> None:
    """Disabilita un provider per questa sessione (anche se già caricato)."""
    DISABLED[name] = reason
    REGISTRY.pop(name, None)
    UNAVAILABLE[name] = f"disabilitato: {reason}"

_PACKAGE = sources.__name__
_SKIP_MODULES = {"base"}
_NOT_PROVIDERS: set[str] = set()
"""Moduli di sources/ che hanno dichiarato ``__provider__ = False`` (helper puri)."""


def available_modules() -> list[str]:
    """Nomi dei moduli provider presenti su disco (senza importarli).

    Un modulo con prefisso ``_`` o che dichiara ``__provider__ = False`` è un
    helper, non un provider.
    """
    names = []
    for info in pkgutil.iter_modules(sources.__path__):
        if info.name in _SKIP_MODULES or info.name in _NOT_PROVIDERS or info.name.startswith("_"):
            continue
        names.append(info.name)
    return sorted(names)


UNAVAILABLE_CLASSES: dict[str, type[AudioSource]] = {}
"""Classi dei provider il cui ``__init__`` ha sollevato ``ProviderUnavailable``:
servono a riconoscere gli URL che quel provider avrebbe gestito."""

UNAVAILABLE_HINTS: dict[str, str] = {}
"""Hint originale del provider non disponibile (``nome -> hint``): nel dispatch
viene messo prima di quello generico, mai appiattito nel messaggio."""


def _instantiate(module: ModuleType, module_name: str) -> list[AudioSource]:
    found: list[AudioSource] = []
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__:
            continue
        if not issubclass(cls, AudioSource) or inspect.isabstract(cls):
            continue
        if cls in (AudioSource, MetadataSource):
            continue
        # "*" = default per tutti i provider (es. --cookies dalla CLI/façade),
        # sovrascritti dalla tabella [options.<nome>] del provider.
        merged = {**(OPTIONS.get("*") or {}), **(OPTIONS.get(module_name) or {})}
        try:
            instance = cls(merged)
        except ProviderUnavailable:
            UNAVAILABLE_CLASSES[module_name] = cls
            raise
        if not instance.name:
            instance.name = module_name
        found.append(instance)
    return found


def _register_classes_only(name: str) -> None:
    """Importa ``sources/<name>.py`` senza istanziare nulla e registra la classe
    in ``UNAVAILABLE_CLASSES``: serve ai provider disabilitati per rivendicare
    i propri URL (``can_handle`` di norma guarda solo l'URL)."""
    if name in UNAVAILABLE_CLASSES:
        return
    try:
        module = importlib.import_module(f"{_PACKAGE}.{name}")
    except Exception as exc:  # noqa: BLE001
        log.debug("provider disabilitato '%s' non importabile: %s", name, exc)
        return
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ == module.__name__ and issubclass(cls, AudioSource) and not inspect.isabstract(cls) \
                and cls not in (AudioSource, MetadataSource):
            UNAVAILABLE_CLASSES[name] = cls
            return


def _claimed_by_unavailable(url: str) -> str | None:
    """Nome del provider non disponibile che rivendicherebbe ``url`` (se uno
    c'è): si usa un'istanza senza ``__init__`` perché ``can_handle`` di norma
    guarda solo l'URL."""
    for name, cls in UNAVAILABLE_CLASSES.items():
        if getattr(cls, "fallback", False):
            continue  # un passthrough non "rivendica" niente
        try:
            if cls.can_handle(cls.__new__(cls), url):
                return name
        except Exception:  # noqa: BLE001 - un can_handle che dipende dall'init non è affidabile
            continue
    return None


def load(name: str) -> AudioSource | None:
    """Carica (se non già caricato) il provider ``name`` da ``sources/<name>.py``.

    Restituisce l'istanza, oppure ``None`` se il modulo manca o è rotto
    (il motivo è in ``UNAVAILABLE[name]``).
    """
    if name in REGISTRY:
        return REGISTRY[name]
    if name in DISABLED:
        UNAVAILABLE[name] = f"disabilitato: {DISABLED[name]}"
        _register_classes_only(name)  # così un URL suo viene rivendicato con il motivo, non girato al fallback
        return None
    if name in UNAVAILABLE:
        return None
    module_path = f"{_PACKAGE}.{name}"
    try:
        module = importlib.import_module(module_path)
        if getattr(module, "__provider__", True) is False:
            _NOT_PROVIDERS.add(name)
            log.debug("sources/%s.py è un helper (__provider__ = False), non un provider", name)
            return None
        instances = _instantiate(module, name)
    except ProviderUnavailable as exc:
        UNAVAILABLE[name] = str(exc)
        if exc.hint:
            UNAVAILABLE_HINTS[name] = exc.hint  # l'hint del provider vince su quello del dispatch
        log.warning("Provider '%s' non disponibile: %s", name, exc)
        return None
    except ModuleNotFoundError as exc:
        if exc.name == module_path:
            UNAVAILABLE[name] = f"nessun modulo sources/{name}.py"
        else:
            UNAVAILABLE[name] = (
                f"dipendenza mancante '{exc.name}' "
                f"(installa il pacchetto richiesto da sources/{name}.py)"
            )
        log.warning("Provider '%s' non caricato: %s", name, UNAVAILABLE[name])
        return None
    except Exception as exc:  # noqa: BLE001 - un provider rotto non deve fermare l'avvio
        UNAVAILABLE[name] = f"{type(exc).__name__}: {exc}"
        log.warning("Provider '%s' rotto: %s", name, UNAVAILABLE[name], exc_info=log.isEnabledFor(logging.DEBUG))
        return None

    if not instances:
        UNAVAILABLE[name] = f"sources/{name}.py non definisce nessuna sottoclasse concreta di AudioSource"
        log.warning("Provider '%s': %s", name, UNAVAILABLE[name])
        return None

    for instance in instances:
        if instance.name in REGISTRY and REGISTRY[instance.name] is not instance:
            log.warning("Provider '%s' definito due volte, tengo il primo", instance.name)
            continue
        REGISTRY[instance.name] = instance
    return REGISTRY.get(name) or instances[0]


def load_all(order: list[str] | None = None) -> list[AudioSource]:
    """Carica tutti i provider trovati in ``sources/`` e li restituisce in
    ordine di priorità: prima quelli elencati in ``order`` (nella sequenza
    data), poi gli altri in ordine alfabetico. I provider rotti sono esclusi.
    """
    order = list(order or [])
    for name in order + available_modules():
        load(name)
    return ordered(order)


def ordered(order: list[str] | None = None) -> list[AudioSource]:
    """Provider già caricati, ordinati per priorità."""
    order = list(order or [])
    rank = {name: i for i, name in enumerate(order)}
    return sorted(
        REGISTRY.values(),
        key=lambda s: (rank.get(s.name, len(rank)), s.name),
    )


def dispatch(url: str, forced: str | None = None, order: list[str] | None = None) -> AudioSource:
    """Sceglie il provider per ``url``.

    Con ``forced`` usa esattamente quel provider (caricandolo se serve) e
    fallisce con un messaggio azionabile se non esiste o non è disponibile.
    Altrimenti scorre i provider nell'ordine di priorità e usa il primo il cui
    ``can_handle(url)`` è vero.
    """
    if forced:
        source = load(forced)
        if source is None:
            reason = UNAVAILABLE.get(forced, "provider sconosciuto")
            raise ProviderUnavailable(
                f"Il provider '{forced}' non è utilizzabile: {reason}",
                hint=f"Provider disponibili: {', '.join(available_modules()) or 'nessuno'}. "
                "Controlla il nome passato a --provider.",
                provider=forced,
            )
        return source

    loaded_sources = load_all(order)
    primary = [s for s in loaded_sources if not s.fallback]
    fallbacks = [s for s in loaded_sources if s.fallback]

    def first_match(candidates: list[AudioSource]) -> AudioSource | None:
        for source in candidates:
            try:
                if source.can_handle(url):
                    return source
            except Exception as exc:  # noqa: BLE001
                log.warning("Provider '%s' ha fallito can_handle(%s): %s", source.name, url, exc)
        return None

    match = first_match(primary)
    if match is not None:
        return match

    claimant = _claimed_by_unavailable(url)
    if claimant is not None:
        dispatch_hint = ("Risolvi la causa indicata (credenziali, dipendenze) e rilancia, "
                         "oppure forza un altro provider con --provider <nome>.")
        provider_hint = UNAVAILABLE_HINTS.get(claimant)
        raise ProviderUnavailable(
            f"L'URL è gestito dal provider '{claimant}', che non è disponibile: {UNAVAILABLE.get(claimant, '?')}",
            hint=f"{provider_hint} — {dispatch_hint}" if provider_hint else dispatch_hint,  # provider per primo
            provider=claimant,
        )

    match = first_match(fallbacks)
    if match is not None:
        return match

    loaded = ", ".join(s.name for s in ordered(order)) or "nessuno"
    broken = "; ".join(f"{n}: {r}" for n, r in UNAVAILABLE.items())
    hint = f"Provider caricati: {loaded}."
    if broken:
        hint += f" Provider non disponibili: {broken}."
    hint += " Puoi forzarne uno con --provider <nome>."
    raise ProviderError(f"Nessun provider sa gestire l'URL: {url}", hint=hint)


def audio_provider_for(source: AudioSource, order: list[str] | None = None) -> AudioSource:
    """Provider a cui delegare l'audio di una ``MetadataSource``."""
    order = order or PRIORITY
    if source.supports_direct_audio:
        return source
    wanted = getattr(source, "audio_provider", None)
    if wanted:
        target = load(wanted)
        if target is None:
            raise ProviderUnavailable(
                f"'{source.name}' delega l'audio a '{wanted}', che non è disponibile: "
                f"{UNAVAILABLE.get(wanted, 'provider sconosciuto')}",
                hint="Imposta audio_provider su un provider funzionante o correggi quello indicato.",
                provider=source.name,
            )
        if not target.supports_direct_audio:
            raise ProviderError(
                f"'{wanted}' non scarica audio: non può ricevere la delega di '{source.name}'",
                provider=source.name,
            )
        if not target.accepts_delegation:
            raise ProviderError(
                f"'{wanted}' scarica solo URL diretti e non accetta deleghe: "
                f"non può fare da audio_provider per '{source.name}'",
                hint="Scegli un provider con ricerca (accepts_delegation = True).",
                provider=source.name,
            )
        return target
    for candidate in load_all(order):
        if candidate.supports_direct_audio and candidate.accepts_delegation:
            return candidate
    raise ProviderUnavailable(
        f"Nessun provider con audio diretto e ricerca a cui delegare '{source.name}'",
        hint="Aggiungi in sources/ un provider con supports_direct_audio = True e accepts_delegation = True.",
        provider=source.name,
    )


def reset() -> None:
    """Svuota il registry (usato dai test)."""
    for source in REGISTRY.values():
        try:
            source.close()
        except Exception:  # noqa: BLE001
            pass
    REGISTRY.clear()
    UNAVAILABLE.clear()
    UNAVAILABLE_CLASSES.clear()
    UNAVAILABLE_HINTS.clear()
    OPTIONS.clear()
    PRIORITY.clear()
    DISABLED.clear()
    _NOT_PROVIDERS.clear()
