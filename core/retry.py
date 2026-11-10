"""Backoff esponenziale con jitter e circuit breaker per provider.

Si ritenta solo su errori marcati ``retryable`` o con HTTP 429 / 5xx. Dopo
``threshold`` errori consecutivi il breaker di quel provider si apre e ogni
nuova chiamata fallisce subito con un messaggio chiaro, finché non scade il
``cooldown`` (half-open: una chiamata di prova lo richiude se va bene).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, TypeVar

from sources.base import ProviderError, ProviderUnavailable

log = logging.getLogger("grabmedia.retry")

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({429, *range(500, 600)})


class CircuitOpen(ProviderError):
    """Il provider ha superato la soglia di errori consecutivi."""


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ProviderUnavailable):
        return False
    if isinstance(exc, ProviderError):
        if exc.status_code in RETRYABLE_STATUS:
            return True
        return bool(exc.retryable)
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return isinstance(status, int) and status in RETRYABLE_STATUS


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 30.0, jitter: float = 0.5) -> float:
    """Ritardo prima del tentativo ``attempt`` (1-based): ``base * 2**(attempt-1)``
    limitato a ``cap``, più jitter uniforme in ``[-jitter, +jitter] * delay``."""
    if attempt < 1:
        return 0.0
    delay = min(cap, base * (2 ** (attempt - 1)))
    if jitter:
        delay += delay * random.uniform(-jitter, jitter)
    return max(0.0, delay)


def retry_after_from(exc: BaseException) -> float | None:
    """Rispetta un eventuale ``Retry-After`` esposto dall'eccezione (in secondi)."""
    value = getattr(exc, "retry_after", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class CircuitBreaker:
    def __init__(self, name: str, *, threshold: int = 5, cooldown: float = 60.0) -> None:
        self.name = name
        self.threshold = max(1, threshold)
        self.cooldown = max(0.0, cooldown)
        self._failures = 0
        self._unknown = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._is_open_locked()

    def _is_open_locked(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown:
            return False  # half-open: lascia passare una prova
        return True

    def check(self) -> None:
        """Solleva ``CircuitOpen`` se il breaker è aperto."""
        with self._lock:
            if self._is_open_locked():
                remaining = self.cooldown - (time.monotonic() - self._opened_at)  # type: ignore[operator]
                raise CircuitOpen(
                    f"circuit breaker aperto dopo {self._failures} errori consecutivi",
                    hint=f"Riprova tra {remaining:.0f}s, oppure controlla credenziali/connessione "
                    f"del provider '{self.name}' e rilancia.",
                    provider=self.name,
                )

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._unknown = 0
            self._opened_at = None

    def _open_locked(self, count: int, what: str) -> None:
        if self._opened_at is None or not self._is_open_locked():
            log.error("Provider '%s': %d %s consecutivi, circuit breaker aperto per %.0fs",
                      self.name, count, what, self.cooldown)
        self._opened_at = time.monotonic()

    def record_failure(self) -> None:
        """Errore transitorio classificato (429, 5xx, rete): conta verso la soglia
        e azzera il contatore degli sconosciuti."""
        with self._lock:
            self._unknown = 0
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_locked(self._failures, "errori transitori")

    def record_unknown(self) -> None:
        """Errore NON classificato: contatore separato, apre il breaker alla
        soglia; si azzera su qualsiasi errore classificato o su un successo."""
        with self._lock:
            self._unknown += 1
            if self._unknown >= self.threshold:
                self._open_locked(self._unknown, "errori non classificati")

    def record_classified(self) -> None:
        """Errore permanente classificato (404, geo-block, ...): non conta, ma
        azzera gli sconosciuti consecutivi."""
        with self._lock:
            self._unknown = 0

    @property
    def unknown_failures(self) -> int:
        return self._unknown

    def reset(self) -> None:
        self.record_success()


class Breakers:
    """Un ``CircuitBreaker`` per provider, creato al primo uso."""

    def __init__(self, *, threshold: int = 5, cooldown: float = 60.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._items: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            if name not in self._items:
                self._items[name] = CircuitBreaker(name, threshold=self.threshold, cooldown=self.cooldown)
            return self._items[name]


def call_with_retry(
    fn: Callable[[], T],
    *,
    provider: str,
    breaker: CircuitBreaker | None = None,
    max_attempts: int = 4,
    base: float = 1.0,
    cap: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    what: str = "operazione",
) -> T:
    """Esegue ``fn`` con retry su 429/5xx/errori retryable e circuit breaker."""
    last: BaseException | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        if breaker is not None:
            breaker.check()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            retryable = is_retryable(exc)
            # Il breaker conta gli errori transitori e, su un contatore separato,
            # gli errori non classificati consecutivi; un 404 o un geo-block
            # non dicono niente sulla salute del provider e non lo aprono.
            if breaker is not None:
                if getattr(exc, "kind", None) == "unknown":
                    breaker.record_unknown()
                elif retryable:
                    breaker.record_failure()
                else:
                    breaker.record_classified()
            # Un errore può limitare da sé i propri retry (es. non classificato -> 1).
            limit = getattr(exc, "retry_limit", None)
            allowed = max_attempts if limit is None else min(max_attempts, 1 + max(0, int(limit)))
            if not retryable or attempt >= allowed:
                raise
            if breaker is not None and breaker.is_open:
                raise
            delay = retry_after_from(exc)
            if delay is None:
                delay = backoff_delay(attempt, base=base, cap=cap)
            log.warning(
                "[%s] %s fallita (tentativo %d/%d): %s. Riprovo tra %.1fs",
                provider, what, attempt, max_attempts, exc, delay,
            )
            sleep(delay)
            continue
        if breaker is not None:
            breaker.record_success()
        return result
    assert last is not None
    raise last
