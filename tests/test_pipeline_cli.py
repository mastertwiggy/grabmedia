import sys
from pathlib import Path

import pytest

from core import cli, registry, state
from core.config import Config
from core.pipeline import Pipeline, format_plan


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        out_dir=tmp_path / "out",
        state_db=tmp_path / "state.sqlite3",
        priority=["fakeok", "fakemeta", "fakeflaky"],
        backoff_base=0.0,
        backoff_cap=0.0,
    )


def _fake(name: str):
    return sys.modules[f"sources.{name}"]


def test_download_album_with_naming_and_state(fake_sources, cfg):
    p = Pipeline(cfg)
    summary = p.run(["https://fakeok.test/album"])
    p.close()
    assert summary.ok and len(summary.done) == 3
    files = sorted(f.name for f in (cfg.out_dir / "Artist" / "Album").iterdir())
    assert files == ["01 - Song 1.ogg", "02 - Song 2.ogg", "03 - Song 3.ogg"]
    db = state.StateDB(cfg.state_db)
    assert db.counts() == {state.DONE: 3}
    assert all(r.attempts == 1 and r.match_hash for r in db.list())
    db.close()
    assert all(q == "best" for _, q in _fake("fakeok").CALLS)


def test_resume_skips_done_tracks_and_overwrite_redoes(fake_sources, cfg):
    Pipeline(cfg).run(["https://fakeok.test/album"])
    calls = _fake("fakeok").CALLS
    before = len(calls)
    summary = Pipeline(cfg).run(["https://fakeok.test/album"])
    assert len(summary.skipped) == 3 and not summary.done and len(calls) == before
    assert all(i.reason == "già scaricata" for i in summary.skipped)

    summary = Pipeline(cfg, overwrite=True).run(["https://fakeok.test/album"])
    assert len(summary.done) == 3 and len(calls) == before + 3


def test_quality_change_invalidates_resume(fake_sources, cfg):
    Pipeline(cfg).run(["https://fakeok.test/one"])
    cfg.quality["fakeok"] = "128k"
    summary = Pipeline(cfg).run(["https://fakeok.test/one"])
    # il file esiste già con lo stesso stem -> skip "file già presente", non "già scaricata"
    assert summary.skipped[0].reason == "file già presente"
    cfg.overwrite = True
    summary = Pipeline(cfg).run(["https://fakeok.test/one"])
    assert summary.done and _fake("fakeok").CALLS[-1][1] == "128k"


def test_existing_file_without_state_is_skipped(fake_sources, cfg):
    target = cfg.out_dir / "Artist" / "Album" / "01 - Song 1.flac"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    summary = Pipeline(cfg).run(["https://fakeok.test/one"])
    assert summary.skipped and summary.skipped[0].result_path == target
    assert not _fake("fakeok").CALLS


def test_dry_run_downloads_nothing_and_writes_no_state(fake_sources, cfg):
    p = Pipeline(cfg, dry_run=True)
    summary = p.run(["https://fakeok.test/album", "https://fakemeta.test/x"])
    assert len(summary.planned) == 4 and not summary.done
    assert all(i.action == "download" for i in summary.planned)
    meta = summary.planned[-1]
    assert meta.source == "fakemeta" and meta.audio_provider == "fakeok"
    assert meta.dest == cfg.out_dir / "Meta Artist" / "Meta Album" / "07 - Meta Song"
    assert not cfg.out_dir.exists() and not cfg.state_db.exists()
    assert not _fake("fakeok").CALLS
    text = format_plan(summary, dry_run=True)
    assert "PIANO" in text and "DL   Meta Artist - Meta Song" in text and "[fakeok, best]" in text


def test_metadata_source_delegates_audio(fake_sources, cfg):
    summary = Pipeline(cfg).run(["https://fakemeta.test/x"])
    assert summary.ok and summary.done[0].result_path.name == "07 - Meta Song.ogg"
    key, _ = _fake("fakeok").CALLS[-1]
    assert key == "fakemeta:m1"  # il provider audio riceve la Track di metadati


def test_forced_provider(fake_sources, cfg):
    summary = Pipeline(cfg, forced_provider="fakeok").run(["https://nothing-matches.example/"])
    assert summary.ok and summary.done
    summary = Pipeline(cfg, forced_provider="fakebroken").run(["https://x"])
    assert summary.url_errors and "fakebroken" in summary.url_errors[0][1]


def test_retry_then_success_and_failure_recorded(fake_sources, cfg):
    flaky = _fake("fakeflaky") if "sources.fakeflaky" in sys.modules else None
    registry.load("fakeflaky")
    flaky = _fake("fakeflaky")
    flaky.FAIL_TIMES["n"] = 2
    summary = Pipeline(cfg).run(["https://fakeflaky.test/t"])
    assert summary.ok and flaky.ATTEMPTS["n"] == 3

    flaky.ATTEMPTS["n"] = 0
    flaky.FAIL_TIMES["n"] = 99
    flaky.STATUS["code"] = 404  # non retryable
    cfg.overwrite = True
    summary = Pipeline(cfg).run(["https://fakeflaky.test/t"])
    assert summary.failed and flaky.ATTEMPTS["n"] == 1
    db = state.StateDB(cfg.state_db)
    rec = db.get("fakeflaky:f1")
    assert rec.status == state.FAILED and "HTTP 404" in rec.error
    db.close()


def test_circuit_breaker_in_pipeline(fake_sources, cfg):
    registry.load("fakeflaky")
    flaky = _fake("fakeflaky")
    flaky.ATTEMPTS["n"] = 0
    flaky.FAIL_TIMES["n"] = 99
    flaky.STATUS["code"] = 503
    cfg.max_attempts = 10
    cfg.circuit_threshold = 5
    summary = Pipeline(cfg).run(["https://fakeflaky.test/t"])
    assert summary.failed and flaky.ATTEMPTS["n"] == 5
    assert "circuit breaker" in summary.failed[0].error or "temporaneo" in summary.failed[0].error


def test_concurrency_semaphore_limits(fake_sources, cfg):
    cfg.concurrency["fakeok"] = 1
    p = Pipeline(cfg)
    assert p.semaphores.get("fakeok")._initial_value == 1  # type: ignore[attr-defined]
    assert p.semaphores.get("other")._initial_value == 2  # type: ignore[attr-defined]


# ------------------------------------------------------------------------- CLI
def _write_cfg(tmp_path: Path) -> Path:
    f = tmp_path / "config.toml"
    f.write_text(
        '[general]\nout_dir = "out"\nstate_db = "st.db"\n'
        '[priority]\norder = ["fakeok", "fakemeta"]\n'
        "[retry]\nbackoff_base = 0.0\nbackoff_cap = 0.0\n",
        encoding="utf-8",
    )
    return f


def test_cli_list_providers(fake_sources, tmp_path, capsys):
    rc = cli.main(["--config", str(_write_cfg(tmp_path)), "--list-providers"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK   fakeok" in out and "metadati -> fakeok" in out
    assert "fakebroken" in out and "non disponibile" in out
    assert "Priorità: fakeok, fakemeta" in out


def test_cli_dry_run_and_provider(fake_sources, tmp_path, capsys):
    cfgp = _write_cfg(tmp_path)
    rc = cli.main(["--config", str(cfgp), "--dry-run", "https://fakeok.test/album"])
    out = capsys.readouterr().out
    assert rc == 0 and "PIANO" in out and out.count("DL   ") == 3
    assert not (tmp_path / "out").exists()

    rc = cli.main(["--config", str(cfgp), "--provider", "fakeok", "--quality", "128k", "https://any"])
    out = capsys.readouterr().out
    assert rc == 0 and "OK   Artist - Song 1" in out
    assert _fake("fakeok").CALLS[-1][1] == "128k"
    assert (tmp_path / "out" / "Artist" / "Album" / "01 - Song 1.ogg").exists()


def test_cli_errors(fake_sources, tmp_path, capsys):
    cfgp = _write_cfg(tmp_path)
    rc = cli.main(["--config", str(cfgp), "--provider", "fakebroken", "https://x"])
    assert rc == 1  # URL in errore -> parziale
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(tmp_path / "missing.toml"), "https://x"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(cfgp)])
    assert exc.value.code == 2
