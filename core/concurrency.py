"""Semafori di concorrenza per provider.

Ogni provider ha il proprio limite (``[concurrency]`` in config.toml); le
tracce di provider diversi non si contendono gli stessi slot.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator


class ProviderSemaphores:
    def __init__(self, limit_for: Callable[[str], int]) -> None:
        self._limit_for = limit_for
        self._items: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def get(self, provider: str) -> threading.BoundedSemaphore:
        with self._lock:
            if provider not in self._items:
                self._items[provider] = threading.BoundedSemaphore(max(1, self._limit_for(provider)))
            return self._items[provider]

    @contextmanager
    def slot(self, provider: str) -> Iterator[None]:
        sem = self.get(provider)
        sem.acquire()
        try:
            yield
        finally:
            sem.release()

    def limits(self) -> dict[str, int]:
        with self._lock:
            return {name: sem._initial_value for name, sem in self._items.items()}  # type: ignore[attr-defined]
