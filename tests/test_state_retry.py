from pathlib import Path

import pytest

from core import retry, state
from sources.base import ProviderError, ProviderUnavailable, Track


def track(pid="1") -> Track:
    return Track("p", pid, f"Song {pid}", ["A"])


# ----------------------------------------------------------------------- state
def test_state_roundtrip_and_resume(tmp_path: Path):
    db = state.StateDB(tmp_path / "s.sqlite3")
    t = track()
    dest = tmp_path / "out" / "Song"
    h = state.match_hash(t, "audio", "best", dest)
    assert h == state.match_hash(t, "audio", "best", dest)
    assert h != state.match_hash(t, "audio", "128k", dest)

    db.upsert(t, status=state.PENDING, match_hash=h)
    rec = db.get(t.key)
    assert rec is not None and rec.status == state.PENDING and rec.attempts == 0
    assert db.is_done(t, h) is None

    out = dest.with_suffix(".ogg")
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x")
    db.mark(t.key, state.DONE, path=out, bump_attempts=True)
    rec = db.get(t.key)
    assert rec.status == state.DONE and rec.attempts == 1 and rec.path == str(out)
    assert db.is_done(t, h) == out
    assert db.is_done(t, "otherhash") is None  # match diverso -> rifare

    out.unlink()
    assert db.is_done(t, h) is None  # file sparito -> rifare
    db.close()


def test_state_reset_running_and_counts(tmp_path: Path):
    db = state.StateDB(tmp_path / "s.sqlite3")
    db.upsert(track("1"), status=state.RUNNING)
    db.upsert(track("2"), status=state.FAILED)
    assert db.reset_running() == 1
    assert db.counts() == {state.PENDING: 1, state.FAILED: 1}
    assert [r.provider_id for r in db.list(state.FAILED)] == ["2"]
    db.forget("p:2")
    assert db.get("p:2") is None
    with pytest.raises(ValueError):
        db.mark("p:1", "weird")
    db.close()


# ----------------------------------------------------------------------- retry
def test_backoff_delay_bounds():
    for attempt in range(1, 10):
        d = retry.backoff_delay(attempt, base=1.0, cap=30.0, jitter=0.5)
        expected = min(30.0, 2 ** (attempt - 1))
        assert expected * 0.5 <= d <= expected * 1.5
    assert retry.backoff_delay(3, base=1.0, cap=30.0, jitter=0) == 4.0
    assert retry.backoff_delay(0) == 0.0


def test_is_retryable():
    assert retry.is_retryable(ProviderError("x", status_code=429))
    assert retry.is_retryable(ProviderError("x", status_code=503))
    assert retry.is_retryable(ProviderError("x", retryable=True))
    assert not retry.is_retryable(ProviderError("x", status_code=404))
    assert not retry.is_retryable(ProviderError("x"))
    assert not retry.is_retryable(ProviderUnavailable("x"))
    assert retry.is_retryable(TimeoutError())
    assert not retry.is_retryable(ValueError())


def test_call_with_retry_recovers_after_5xx():
    calls = {"n": 0}
    sleeps: list[float] = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("down", status_code=502)
        return "ok"

    breaker = retry.CircuitBreaker("p", threshold=5)
    assert retry.call_with_retry(fn, provider="p", breaker=breaker, max_attempts=4, sleep=sleeps.append) == "ok"
    assert calls["n"] == 3 and len(sleeps) == 2
    assert breaker.failures == 0  # il successo azzera


def test_call_with_retry_gives_up_and_respects_retry_after():
    sleeps: list[float] = []

    def fn():
        err = ProviderError("rate", status_code=429)
        err.retry_after = 7  # type: ignore[attr-defined]
        raise err

    with pytest.raises(ProviderError):
        retry.call_with_retry(fn, provider="p", max_attempts=3, sleep=sleeps.append)
    assert sleeps == [7.0, 7.0]


def test_call_with_retry_no_retry_on_client_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ProviderError("nope", status_code=404)

    with pytest.raises(ProviderError):
        retry.call_with_retry(fn, provider="p", max_attempts=5, sleep=lambda s: None)
    assert calls["n"] == 1


def test_circuit_breaker_opens_after_threshold_and_half_opens(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(retry.time, "monotonic", lambda: now["t"])
    cb = retry.CircuitBreaker("p", threshold=5, cooldown=60)
    for _ in range(4):
        cb.record_failure()
        cb.check()  # ancora chiuso
    cb.record_failure()
    assert cb.is_open
    with pytest.raises(retry.CircuitOpen) as exc:
        cb.check()
    assert "5 errori consecutivi" in str(exc.value) and "Riprova" in str(exc.value)
    now["t"] += 61
    assert not cb.is_open  # half-open
    cb.check()
    cb.record_success()
    assert cb.failures == 0 and not cb.is_open


def test_call_with_retry_stops_when_breaker_opens():
    cb = retry.CircuitBreaker("p", threshold=2, cooldown=60)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ProviderError("down", status_code=500)

    with pytest.raises(ProviderError):
        retry.call_with_retry(fn, provider="p", breaker=cb, max_attempts=10, sleep=lambda s: None)
    assert calls["n"] == 2  # il breaker si apre al secondo errore e ferma i retry
    with pytest.raises(retry.CircuitOpen):
        retry.call_with_retry(fn, provider="p", breaker=cb, max_attempts=10, sleep=lambda s: None)
    assert calls["n"] == 2


def test_breakers_registry_is_per_provider():
    b = retry.Breakers(threshold=1)
    b.get("a").record_failure()
    assert b.get("a").is_open and not b.get("b").is_open
