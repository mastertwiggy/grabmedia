import os
from pathlib import Path

import pytest

from core import config


def test_defaults_when_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", tmp_path / "nope.toml")
    cfg = config.load()
    assert cfg.naming == "{artist}/{album}/{track_no:02d} - {title}"
    assert cfg.concurrency_for("anything") == 2
    assert cfg.quality_for("anything") == "best"
    assert cfg.overwrite is False


def test_load_full_file(tmp_path: Path):
    f = tmp_path / "config.toml"
    f.write_text(
        """
[general]
out_dir = "music"
naming = "{albumartist}/{title}"
overwrite = true
state_db = "st.db"
[priority]
order = ["alpha", "beta"]
[concurrency]
default = 3
alpha = 1
[quality]
default = "320k"
beta = "flac"
[retry]
max_attempts = 2
backoff_base = 0.5
circuit_threshold = 3
""",
        encoding="utf-8",
    )
    cfg = config.load(f)
    assert cfg.out_dir == (tmp_path / "music").resolve()
    assert cfg.state_db == (tmp_path / "st.db").resolve()
    assert cfg.naming == "{albumartist}/{title}" and cfg.overwrite is True
    assert cfg.priority == ["alpha", "beta"]
    assert cfg.concurrency_for("alpha") == 1 and cfg.concurrency_for("beta") == 3
    assert cfg.quality_for("beta") == "flac" and cfg.quality_for("alpha") == "320k"
    assert cfg.max_attempts == 2 and cfg.backoff_base == 0.5 and cfg.circuit_threshold == 3
    assert cfg.source_path == f


@pytest.mark.parametrize(
    "body, fragment",
    [
        ('[general]\nnaming = "no-title"', "{title}"),
        ("[general]\noverwrite = 1", "true o false"),
        ("[concurrency]\ndefault = 0", ">= 1"),
        ('[priority]\norder = "x"', "lista"),
        ("[quality]\ndefault = 3", "stringa"),
        ("[retry]\nbackoff_base = -1", ">= 0"),
        ("general = [", "TOML"),
    ],
)
def test_invalid_config_messages(tmp_path: Path, body: str, fragment: str):
    f = tmp_path / "config.toml"
    f.write_text(body, encoding="utf-8")
    with pytest.raises(config.ConfigError, match=fragment):
        config.load(f)


def test_missing_explicit_path(tmp_path: Path):
    with pytest.raises(config.ConfigError, match="non trovato"):
        config.load(tmp_path / "missing.toml")


def test_load_env(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# commento\n"
        "PLAIN=value\n"
        'QUOTED="a b # c"\n'
        "export EXPORTED='x'\n"
        "INLINE=val # commento\n"
        "ALREADY=new\n"
        "broken line\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALREADY", "old")
    for key in ("PLAIN", "QUOTED", "EXPORTED", "INLINE"):
        monkeypatch.delenv(key, raising=False)
    loaded = config.load_env(env)
    assert loaded == {"PLAIN": "value", "QUOTED": "a b # c", "EXPORTED": "x", "INLINE": "val", "ALREADY": "new"}
    assert os.environ["PLAIN"] == "value" and os.environ["QUOTED"] == "a b # c"
    assert os.environ["ALREADY"] == "old"
    config.load_env(env, override=True)
    assert os.environ["ALREADY"] == "new"
    assert config.load_env(tmp_path / "absent") == {}


def test_env_example_documents_spotify_only():
    text = (config.PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SPOTIFY_CLIENT_ID=" in text and "SPOTIFY_CLIENT_SECRET=" in text
    assert "SPOTIFY_REDIRECT_URI=" in text and "developer.spotify.com" in text
    # nessun valore reale committato
    for line in text.splitlines():
        if line.startswith("SPOTIFY_CLIENT"):
            assert line.endswith("=")


def test_core_has_no_service_names():
    forbidden = ("spotify", "deezer", "tidal", "qobuz", "apple", "youtube", "soundcloud", "bandcamp")
    for py in (config.PROJECT_ROOT / "core").glob("*.py"):
        text = py.read_text(encoding="utf-8").lower()
        for word in forbidden:
            assert word not in text, f"{py.name} contiene '{word}'"
