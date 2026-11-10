"""Brani con più artisti e metadati musicali del candidato.

Casi reali dalla playlist scaricata il 2026-11-10: brani saltati a 0,70 con il
candidato giusto in mano, perché il confronto cercava la stringa unita
"Artista A, Artista B" e ignorava i metadati musicali (``artists``) dei video
"Topic", il cui canale può chiamarsi in qualunque modo."""

from __future__ import annotations

from pathlib import Path

import pytest
import yt_dlp

from sources import _ytdlp, matching
from sources.base import NoMatch, Track
from sources.matching import DEFAULT_THRESHOLD, artist_confirmed, pick


def cand(id_: str, title: str, uploader: str, duration: float = 180.0, **extra) -> dict:
    return {"id": id_, "title": title, "uploader": uploader, "duration": duration, **extra}


ARTISTS = ["Max Niklas", "Jasper Junior"]
QUERY = "Max Niklas, Jasper Junior Wild Ones"


def test_any_of_the_track_artists_confirms_the_candidate():
    for uploader in ("Max Niklas", "Jasper Junior", "Jasper Junior - Topic", "MaxNiklasVEVO"):
        best, score = pick(QUERY, [cand("x", "Wild Ones", uploader)], 180_000, title="Wild Ones", artists=ARTISTS)
        assert best is not None and score >= 0.9, uploader


def test_joined_artist_string_alone_was_the_bug():
    """Con la sola stringa unita (vecchia chiamata) lo stesso candidato resta a 0,70:
    è il motivo per cui i provider ora passano ``artists``."""
    candidate = cand("x", "Wild Ones", "Max Niklas")
    best, score = pick(QUERY, [candidate], 180_000, artist=", ".join(ARTISTS), title="Wild Ones")
    assert best is None and score == pytest.approx(0.70)
    best, score = pick(QUERY, [candidate], 180_000, artist=", ".join(ARTISTS), title="Wild Ones", artists=ARTISTS)
    assert best is not None and score >= DEFAULT_THRESHOLD


def test_topic_channel_with_music_metadata_is_confirmed():
    """Caso reale: 'Papi Chulo', canale 'Release - Topic', artists=['Max Niklas']."""
    real = cand("t", "Papi Chulo", "Release - Topic", 121.0, artists=["Max Niklas"], creator="Max Niklas", album="Papi Chulo")
    best, score = pick("Max Niklas Papi Chulo", [real], 121_000, title="Papi Chulo", artists=["Max Niklas"])
    assert best is not None and score >= 0.9
    assert artist_confirmed(real, artists=["Max Niklas"])
    # anche con la vecchia chiamata a stringa singola: i metadati del candidato contano
    assert pick("Max Niklas Papi Chulo", [real], 121_000, artist="Max Niklas", title="Papi Chulo")[0] is not None
    # metadati in forma di stringa o solo "creator"
    assert artist_confirmed(cand("a", "x", "Etichetta - Topic", artist="MNRT, Max Niklas"), artists=["Max Niklas"])
    assert artist_confirmed(cand("b", "x", "Etichetta - Topic", creator="Max Niklas"), artists=["Max Niklas"])


def test_unrelated_uploader_and_metadata_still_fall_below_threshold():
    stranger = cand("s", "Wild Ones", "RandomChannel", artists=["Flo Rida", "Sia"])  # stesso titolo, altra canzone
    best, score = pick(QUERY, [stranger], 180_000, title="Wild Ones", artists=ARTISTS)
    assert best is None and score < DEFAULT_THRESHOLD
    assert not artist_confirmed(stranger, artists=ARTISTS)
    assert not artist_confirmed(cand("n", "x", "Chiunque"), artists=[])


def test_confirmed_artist_beats_same_title_from_stranger():
    stranger = cand("s", "Wild Ones", "RandomChannel")
    topic = cand("t", "Wild Ones", "Release - Topic", artists=["Max Niklas", "Jasper Junior"])
    for order in ([stranger, topic], [topic, stranger]):
        assert pick(QUERY, order, 180_000, title="Wild Ones", artists=ARTISTS)[0]["id"] == "t"


def test_artist_only_in_title_gets_the_smaller_bonus():
    in_title = cand("a", "Max Niklas - Wild Ones", "House Nation")
    confirmed = cand("b", "Wild Ones", "Max Niklas")
    s_title = matching.score(QUERY, in_title, None, title="Wild Ones", artists=ARTISTS)
    s_conf = matching.score(QUERY, confirmed, None, title="Wild Ones", artists=ARTISTS)
    assert s_conf > s_title >= DEFAULT_THRESHOLD


def test_bad_versions_are_still_rejected_with_a_confirmed_artist():
    live = cand("l", "Wild Ones (Live)", "Max Niklas")
    assert pick(QUERY, [live], 180_000, title="Wild Ones", artists=ARTISTS)[0] is None


def test_names_with_commas_are_not_split():
    creator = cand("c", "See You Again", "Tyler, The Creator")
    assert artist_confirmed(creator, artists=["Tyler, The Creator"])
    assert not artist_confirmed(cand("d", "See You Again", "The Creator Fans"), artists=["Tyler, The Creator"])


# ------------------------------------------------------- provider: passano artists
@pytest.fixture
def opts(tmp_path: Path) -> dict:
    d = tmp_path / "ffmpeg"
    d.mkdir()
    (d / "ffmpeg.exe").write_bytes(b"")
    (d / "ffmpeg").write_bytes(b"")
    return {"ffmpeg_location": str(d)}


@pytest.fixture
def search_results(monkeypatch):
    results: dict[str, list[dict]] = {}

    def extract_info(self, target, download=False, **kw):  # noqa: ANN001
        for prefix, entries in results.items():
            if target.startswith(prefix):
                return {"entries": entries}
        raise yt_dlp.utils.DownloadError(f"ERROR: nessun handler per {target}")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    return results


def test_youtube_search_matches_multi_artist_track(opts, search_results):
    from sources.youtube import YoutubeSource

    search_results["ytsearch5:"] = [
        cand("stranger000", "Wild Ones", "RandomChannel", 180.0),
        cand("topic000000", "Wild Ones", "Release - Topic", 180.0, artists=["Max Niklas", "Jasper Junior"]),
    ]
    track = Track("spotify_embed", "id", "Wild Ones", list(ARTISTS), duration_ms=180_000)
    yt = YoutubeSource(opts)
    assert yt._search(track) == "topic000000"  # noqa: SLF001
    assert track.extra["match_score"] >= 0.9
    # senza nessuna conferma dell'artista resta un NoMatch
    search_results["ytsearch5:"] = [cand("stranger000", "Wild Ones", "RandomChannel", 180.0)]
    with pytest.raises(NoMatch):
        yt._search(Track("spotify_embed", "id2", "Wild Ones", list(ARTISTS), duration_ms=180_000))  # noqa: SLF001


def test_soundcloud_search_matches_on_second_artist_channel(opts, search_results):
    from sources.soundcloud import SoundcloudSource

    search_results["scsearch10:"] = [
        {**cand("11", "Wild Ones", "Jasper Junior", 180.0), "webpage_url": "https://soundcloud.com/jasperjunior/wild-ones"},
    ]
    track = Track("spotify_embed", "id", "Wild Ones", list(ARTISTS), duration_ms=180_000)
    sc = SoundcloudSource(opts)
    assert sc._search(track) == "https://soundcloud.com/jasperjunior/wild-ones"  # noqa: SLF001
    assert track.extra["matched_track_id"] == "11"
