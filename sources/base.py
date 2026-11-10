"""Contratto comune a tutti i provider audio.

Il core non conosce nessun servizio: vede solo ``Track`` e ``AudioSource``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

ProgressCallback = Callable[[float, str], None]
"""Callback di avanzamento: ``(percentuale 0-100, messaggio breve)``."""


class ProviderError(Exception):
    """Errore sollevato da un provider.

    ``hint`` dice all'utente cosa fare; ``retryable`` e ``status_code``
    permettono al core di decidere se ritentare (429 / 5xx) o fermarsi.
    ``retry_limit`` limita i nuovi tentativi per questo errore (es. 1 per un
    errore non classificato: un retry, poi è permanente); ``None`` = usa il
    massimo del core. ``kind`` è l'etichetta della classificazione (log).
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool = False,
        status_code: int | None = None,
        provider: str | None = None,
        retry_limit: int | None = None,
        kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.retryable = retryable
        self.status_code = status_code
        self.provider = provider
        self.retry_limit = retry_limit
        self.kind = kind

    def __str__(self) -> str:
        parts = []
        if self.provider:
            parts.append(f"[{self.provider}]")
        parts.append(self.message)
        if self.status_code:
            parts.append(f"(HTTP {self.status_code})")
        text = " ".join(parts)
        if self.hint:
            text += f"\n  -> {self.hint}"
        return text


class ProviderUnavailable(ProviderError):
    """Il provider esiste ma non può funzionare (dipendenza mancante,
    credenziali assenti, binario non trovato). Non è mai retryable."""

    def __init__(self, message: str, *, hint: str | None = None, provider: str | None = None) -> None:
        super().__init__(message, hint=hint, retryable=False, provider=provider)


class NeedsCookies(ProviderError):
    """Il sito chiede un login o una verifica anti-bot: classe TERMINALE.
    Mai retryable, nessun backoff, non consuma tentativi del core e non conta
    per il circuit breaker. L'hint dice come fornire i cookie."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        provider: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, hint=hint, retryable=False, status_code=status_code,
                         provider=provider, retry_limit=0, kind="need_cookies")


class NoMatch(ProviderError):
    """Il provider audio non ha trovato un risultato affidabile per una traccia
    delegata. Non è retryable: la pipeline marca la traccia ``skipped``."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        provider: str | None = None,
        query: str | None = None,
        score: float | None = None,
    ) -> None:
        super().__init__(message, hint=hint, retryable=False, provider=provider)
        self.query = query
        self.score = score


@dataclass(slots=True)
class Track:
    """Metadati di una traccia, indipendenti dal provider che li ha prodotti.

    I campi non noti restano ``None`` (o lista/dict vuoti): nessun provider
    deve inventare valori. ``extra`` ospita dati specifici del provider
    (url di origine, id secondari, query di ricerca, ...).
    """

    provider: str
    provider_id: str
    title: str
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    year: int | None = None
    cover_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Identificatore stabile della traccia (usato dallo stato su SQLite)."""
        return f"{self.provider}:{self.provider_id}"

    @property
    def artist(self) -> str:
        return ", ".join(a for a in self.artists if a)

    @property
    def albumartist(self) -> str | None:
        value = self.extra.get("albumartist")
        if value:
            return str(value)
        return self.artists[0] if self.artists else None

    @property
    def duration_s(self) -> float | None:
        return self.duration_ms / 1000 if self.duration_ms is not None else None

    def display(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


class AudioSource(ABC):
    """Provider capace di riconoscere URL, risolverli in tracce e scaricarle.

    ``options`` sono le impostazioni della tabella ``[options.<nome>]`` di
    config.toml (cookie, percorso di ffmpeg, ...), passate dal registry.
    """

    name: str = ""
    supports_direct_audio: bool = True
    accepts_delegation: bool = True
    """``False`` se il provider non deve mai ricevere tracce di altri provider
    (niente ricerca/matching): non è eleggibile come ``audio_provider``."""
    fallback: bool = False
    """``True`` per un provider di ultima istanza (passthrough): il registry lo
    interroga solo dopo tutti gli altri, anche dopo aver controllato se un
    provider non disponibile rivendica l'URL."""

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options: dict[str, Any] = dict(options or {})

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """``True`` se questo provider sa gestire ``url``."""

    @abstractmethod
    def resolve(self, url: str) -> list[Track]:
        """Espande ``url`` (traccia, album, playlist) in una lista di ``Track``."""

    @abstractmethod
    def download(
        self,
        track: Track,
        dest: Path,
        quality: str,
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        """Scarica ``track`` nel percorso ``dest`` (senza estensione o con
        estensione suggerita) e restituisce il file finale prodotto.

        ``track.provider`` può essere diverso da ``self.name``: in quel caso il
        provider deve cercare l'audio a partire dai metadati (delega).
        """

    def close(self) -> None:
        """Rilascia sessioni HTTP, token, processi. Default: niente."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


class MetadataSource(AudioSource):
    """Provider che risolve solo metadati e delega l'audio a un altro provider.

    ``audio_provider`` è il nome del provider a cui il core passa ogni ``Track``
    risolta; se ``None`` il core usa il primo provider con audio diretto
    nell'ordine di priorità.
    """

    supports_direct_audio = False
    audio_provider: str | None = None

    def delegate(self) -> "AudioSource":
        """Provider audio a cui questa sorgente delega (risolto dal registry)."""
        from core import registry  # import a runtime: core.registry importa questo modulo

        return registry.audio_provider_for(self)

    def download(
        self,
        track: Track,
        dest: Path,
        quality: str,
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        """Delega il download al provider audio, passando la ``Track`` così
        com'è. Le sottoclassi possono sovrascrivere per intercettare
        ``NoMatch`` (log, soglie) e poi chiamare ``super().download``."""
        return self.delegate().download(track, dest, quality, progress_cb)
