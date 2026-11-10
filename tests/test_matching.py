import sys

import pytest

from sources import matching
from sources.matching import normalize, pick

ARTIST = "Daft Punk"
TITLE = "Harder Better Faster Stronger"
QUERY = f"{ARTIST} {TITLE}"
WANT = 224_000  # 3:44


def cand(id_: str, title: str, uploader: str = "Daft Punk", duration: float | None = 224.0, **extra):
    d = {"id": id_, "title": title, "uploader": uploader, "duration": duration}
    d.update(extra)
    return d


def test_module_is_pure():
    assert "yt_dlp" not in sys.modules or "sources.matching" in sys.modules
    assert not hasattr(matching, "YoutubeDL")


def test_normalize_strips_noise():
    assert normalize("Artist - Song (Official Video) [HD] feat. Someone") == "artist song"
    assert normalize("Song Title | Official Lyric Video") == "song title"
    assert normalize("é") == "é"
    assert normalize(None) == ""


def test_correct_match_wins():
    cands = [
        cand("cover", f"{TITLE} (Cover)", uploader="Some Guy"),
        cand("ok", f"{ARTIST} - {TITLE} (Official Video)"),
        cand("other", "Around the World"),
    ]
    best, score = pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)
    assert best is not None and best["id"] == "ok"
    assert score >= 0.9


@pytest.mark.parametrize("bad", ["Cover", "Live", "Remix", "Sped Up", "Nightcore", "8D", "Karaoke", "Instrumental"])
def test_bad_versions_are_discarded(bad: str):
    cands = [cand("bad", f"{ARTIST} - {TITLE} ({bad})")]
    best, score = pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)
    assert best is None and score < matching.DEFAULT_THRESHOLD


def test_bad_version_not_penalized_when_requested():
    cands = [cand("rmx", f"{ARTIST} - {TITLE} (Alive Remix)")]
    best, _ = pick(f"{QUERY} remix", cands, WANT, artist=ARTIST, title=f"{TITLE} remix")
    assert best is not None and best["id"] == "rmx"


def test_incompatible_duration_is_discarded():
    cands = [cand("long", f"{ARTIST} - {TITLE}", duration=224.0 + 60)]
    best, score = pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)
    assert best is None and score < matching.DEFAULT_THRESHOLD


def test_duration_prefers_closest():
    cands = [
        cand("far", f"{ARTIST} - {TITLE}", duration=224.0 + 12),
        cand("near", f"{ARTIST} - {TITLE}", duration=224.0 + 1),
    ]
    best, _ = pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)
    assert best["id"] == "near"


def test_want_duration_none_does_not_penalize():
    cands = [cand("long", f"{ARTIST} - {TITLE}", duration=600.0)]
    best, score = pick(QUERY, cands, None, artist=ARTIST, title=TITLE)
    assert best is not None and best["id"] == "long" and score >= matching.DEFAULT_THRESHOLD
    cands = [cand("nodur", f"{ARTIST} - {TITLE}", duration=None)]
    assert pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)[0] is not None


def test_artist_absent_from_title_and_uploader_falls_below_threshold():
    cands = [cand("anon", TITLE, uploader="RandomChannel")]
    best, score = pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)
    assert best is None and score < matching.DEFAULT_THRESHOLD
    # stesso titolo ma uploader = artista -> accettato
    cands = [cand("topic", TITLE, uploader="Daft Punk - Topic")]
    assert pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)[0]["id"] == "topic"


def test_aggregator_channel_loses_against_official():
    cands = [
        cand("lyrics", f"{ARTIST} - {TITLE}", uploader="Lyrics Hub"),
        cand("official", f"{ARTIST} - {TITLE}", uploader="Daft Punk"),
    ]
    best, _ = pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)
    assert best["id"] == "official"
    cands_rev = list(reversed(cands))
    assert pick(QUERY, cands_rev, WANT, artist=ARTIST, title=TITLE)[0]["id"] == "official"


def test_threshold_configurable_and_empty_candidates():
    cands = [cand("anon", TITLE, uploader="RandomChannel")]
    assert pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE, threshold=0.5)[0] is not None
    assert pick(QUERY, [], WANT) == (None, 0.0)
    assert pick(QUERY, None, WANT) == (None, 0.0)
    assert pick(QUERY, [{"id": "x", "title": ""}], WANT) == (None, 0.0)


def test_pick_has_no_side_effects():
    cands = [cand("ok", f"{ARTIST} - {TITLE}")]
    snapshot = [dict(c) for c in cands]
    best, _ = pick(QUERY, cands, WANT, artist=ARTIST, title=TITLE)
    assert cands == snapshot and best is not cands[0]
