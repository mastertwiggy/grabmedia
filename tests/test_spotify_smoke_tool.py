"""Logica (pura) dello smoke test Spotify: confronto campo per campo tra la
forma delle risposte reali e le fixture sintetiche. Il resolve reale richiede
credenziali e non gira qui."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import spotify_smoke as smoke  # noqa: E402


def test_fixture_shapes_cover_every_field_the_provider_reads():
    """Le fixture dei test offline modellano tutti i campi che il provider legge."""
    fixtures = smoke.fixture_shapes()
    for kind, used in smoke.USED_FIELDS.items():
        missing = [name for name in used if name not in fixtures.get(kind, {})]
        assert missing == [], f"{kind}: campi usati dal provider assenti dalla fixture: {missing}"


def test_compare_identical_shapes_has_no_divergence():
    fixtures = smoke.fixture_shapes()
    report = smoke.merge([smoke.compare(k, fixtures[k], fixtures[k]) for k in smoke.USED_FIELDS])
    assert report == {"critical": [], "fixture_only": [], "missing_in_fixture": [], "real_only": []}


def test_compare_flags_missing_and_retyped_fields_as_critical():
    fixture = {"id": "str", "name": "str", "duration_ms": "int", "album": "dict", "legacy": "str"}
    real = {"id": "str", "name": "str", "duration_ms": "str", "available_markets": "list", "album": "null"}
    report = smoke.compare("track", real, fixture)
    assert any("track.duration_ms: tipo reale str != fixture int" in c for c in report["critical"])
    assert any("track.uri" in c and "assente nella risposta reale" in c for c in report["critical"])
    assert not any("track.album" in c for c in report["critical"])  # null tollerato
    assert report["fixture_only"] == ["track.legacy: nella fixture (str) ma non nella risposta reale"]
    assert report["real_only"] == ["track.available_markets"]
    report = smoke.compare("image", {"url": "str", "width": "int", "height": "int"}, {"url": "str"})
    assert len(report["missing_in_fixture"]) == 2 and report["critical"] == []


def test_shapes_from_real_like_page_including_item_key_variant():
    page = {"items": [{"is_local": False, "added_at": "2026-01-01T00:00:00Z",
                       "item": {"id": "x" * 22, "name": "S", "uri": "spotify:track:x", "type": "track",
                                "artists": [{"name": "A", "id": "1"}], "duration_ms": 1, "track_number": 1,
                                "disc_number": 1, "explicit": False, "is_local": False,
                                "album": {"name": "Al", "release_date": "2020", "total_tracks": 1,
                                          "artists": [{"name": "A"}], "images": [{"url": "u", "width": 1, "height": 1}]}}}],
            "next": None, "total": 1, "offset": 0, "limit": 100}
    shapes = smoke.shapes_from_page(page)
    assert set(shapes) == {"page", "item", "track", "album", "image", "artist"}
    assert shapes["page"]["next"] == "null" and shapes["track"]["duration_ms"] == "int"
    fixtures = smoke.fixture_shapes()
    report = smoke.merge([smoke.compare(k, shapes.get(k, {}), fixtures.get(k, {})) for k in smoke.USED_FIELDS])
    assert report["critical"] == []
    assert smoke.shapes_from_page({"items": []}) == {"page": {"items": "list"}}


def test_scrub_and_cassette_reading(tmp_path: Path):
    response = {"body": {"string": json.dumps({"access_token": "SECRET", "expires_in": 3600})}}
    assert "SECRET" not in smoke._scrub_response(response)["body"]["string"]  # noqa: SLF001
    assert smoke._scrub_response({"body": {"string": "not json"}})["body"]["string"] == "not json"  # noqa: SLF001
    yaml = pytest.importorskip("yaml")
    cassette = tmp_path / "c.yaml"
    cassette.write_text(yaml.safe_dump({"interactions": [
        {"request": {"uri": "https://accounts.spotify.com/api/token"}, "response": {"body": {"string": "{}"}}},
        {"request": {"uri": "https://api.spotify.com/v1/playlists/abc?fields=name"}, "response": {"body": {"string": "{}"}}},
        {"request": {"uri": "https://api.spotify.com/v1/playlists/abc/items?limit=100&offset=0"},
         "response": {"body": {"string": json.dumps({"items": [], "total": 0})}}},
    ]}), encoding="utf-8")
    assert smoke.real_playlist_pages(cassette) == [{"items": [], "total": 0}]


def test_smoke_exits_with_instructions_without_credentials(monkeypatch, capsys, tmp_path: Path):
    from core import config as config_mod

    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(config_mod, "DEFAULT_ENV_PATH", tmp_path / "no.env")
    rc = smoke.main(["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "--cassette", str(tmp_path / "c.yaml")])
    assert rc == 2 and "SPOTIFY_CLIENT_ID" in capsys.readouterr().err
    assert not (tmp_path / "c.yaml").exists()
