from pathlib import Path

import pytest

from core import naming
from sources.base import ProviderError, ProviderUnavailable, Track


def track(**kw) -> Track:
    base = dict(provider="p", provider_id="1", title="Title", artists=["Artist"])
    base.update(kw)
    return Track(**base)


def test_track_properties():
    t = track(artists=["A", "B"], extra={"albumartist": "AA"}, duration_ms=1500)
    assert t.key == "p:1"
    assert t.artist == "A, B"
    assert t.albumartist == "AA"
    assert t.duration_s == 1.5
    assert t.display() == "A, B - Title"
    assert track(artists=[]).albumartist is None
    assert track(artists=[]).display() == "Title"


def test_provider_error_message_is_actionable():
    err = ProviderError("boom", hint="fai X", status_code=503, provider="prov")
    text = str(err)
    assert "[prov]" in text and "boom" in text and "HTTP 503" in text and "fai X" in text
    assert ProviderUnavailable("x").retryable is False
    assert isinstance(ProviderUnavailable("x"), ProviderError)


def test_sanitize_invalid_chars_and_reserved():
    assert naming.sanitize('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert naming.sanitize("  spaced   out . ") == "spaced out"
    assert naming.sanitize("CON") == "CON_"
    assert naming.sanitize("") == "_"
    assert len(naming.sanitize("x" * 500)) == naming.MAX_SEGMENT_LENGTH


def test_sanitize_normalizes_nfc():
    decomposed = "é"  # e + combining acute
    assert naming.sanitize(decomposed) == "é"


def test_render_full_template():
    t = track(album="Album", track_no=3, disc_no=1, year=2020)
    assert naming.render(t, "{artist}/{album}/{disc_no}-{track_no:02d} - {title}") == [
        "Artist", "Album", "1-03 - Title",
    ]
    assert naming.render(t, "{year} - {albumartist}/{title}") == ["2020 - Artist", "Title"]


def test_render_null_fields_are_dropped_cleanly():
    t = track(album=None, track_no=None, year=None)
    assert naming.render(t, "{artist}/{album}/{track_no:02d} - {title}") == ["Artist", "Title"]
    assert naming.render(track(artists=[]), "{artist}/{title}") == ["Unknown Artist", "Title"]


def test_render_slash_inside_values_is_not_a_separator():
    t = track(title="AC/DC Live", album="Back/Forth")
    segs = naming.render(t, "{album}/{title}")
    assert len(segs) == 2
    assert "/" not in segs[0] and "/" not in segs[1]


def test_render_unknown_placeholder_raises():
    with pytest.raises(ValueError, match="Placeholder sconosciuto"):
        naming.render(track(), "{nope}/{title}")


def test_build_creates_directories_and_extension(tmp_path: Path):
    t = track(album="Al", track_no=1)
    path = naming.build(t, "{artist}/{album}/{track_no:02d} - {title}", tmp_path, ext="mp3")
    assert path == tmp_path / "Artist" / "Al" / "01 - Title.mp3"
    assert path.parent.is_dir()
    assert not path.exists()
    assert naming.build(t, "{title}", tmp_path).name == "Title"
