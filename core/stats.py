"""Contatori di esecuzione (thread-safe), senza riferimenti a servizi.

Chi incrementa: ``_ytdlp.download_audio`` (``stream_reextract`` quando l'info
riusato è scaduto e si ri-estrae), la pipeline li stampa nel riepilogo.
"""

from __future__ import annotations

import threading
from collections import Counter

_counter: Counter[str] = Counter()
_lock = threading.Lock()


def increment(name: str, by: int = 1) -> int:
    with _lock:
        _counter[name] += by
        return _counter[name]


def get(name: str) -> int:
    with _lock:
        return _counter.get(name, 0)


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counter)


def reset() -> None:
    with _lock:
        _counter.clear()
