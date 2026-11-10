"""Costruzione del percorso di destinazione a partire da una ``Track``.

Template in stile ``str.format``; i segmenti separati da ``/`` diventano
sottocartelle. Placeholder disponibili: ``artist``, ``albumartist``, ``album``,
``title``, ``track_no``, ``disc_no``, ``year`` (anche con format spec, es.
``{track_no:02d}``). I campi nulli diventano stringa vuota, salvo ``title`` e
``artist`` che ricadono su un valore di default per non produrre nomi vuoti.
"""

from __future__ import annotations

import re
import string
import unicodedata
from pathlib import Path

from sources.base import Track

DEFAULT_TEMPLATE = "{artist}/{album}/{track_no:02d} - {title}"
MAX_SEGMENT_LENGTH = 120  # per componente: titoli/album lunghi non devono rompere i path
_ALLOWED_FIELDS = {"artist", "albumartist", "album", "title", "track_no", "disc_no", "year"}

# Caratteri vietati su Windows (sovrainsieme di quelli vietati su POSIX) + controlli.
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_SEPARATOR = "/"
_PLACEHOLDER_SLASH = "\u2215"  # DIVISION SLASH: slash dentro i valori, non un separatore di cartelle


def sanitize(segment: str, *, replacement: str = "_") -> str:
    """Rende ``segment`` un nome di file/cartella valido su Windows, macOS e Linux."""
    text = unicodedata.normalize("NFC", str(segment))
    text = _INVALID_CHARS.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip().strip(".").strip()
    if len(text) > MAX_SEGMENT_LENGTH:
        text = text[:MAX_SEGMENT_LENGTH].rstrip(" .")
    if text.upper() in _RESERVED_NAMES:
        text = f"{text}{replacement}"
    return text or "_"


class _TrackFormatter(string.Formatter):
    """Formatter tollerante: campi nulli -> '', format spec applicata solo se possibile."""

    def __init__(self, values: dict[str, object]) -> None:
        super().__init__()
        self._values = values

    def get_value(self, key, args, kwargs):  # type: ignore[override]
        if isinstance(key, str):
            if key not in _ALLOWED_FIELDS:
                raise KeyError(key)
            return self._values.get(key)
        return super().get_value(key, args, kwargs)

    def format_field(self, value, format_spec):  # type: ignore[override]
        if value is None or value == "":
            return ""
        try:
            text = format(value, format_spec)
        except (ValueError, TypeError):
            text = str(value)
        return str(text).replace(_SEPARATOR, _PLACEHOLDER_SLASH)


def _values_for(track: Track) -> dict[str, object]:
    return {
        "artist": track.artist or "Unknown Artist",
        "albumartist": track.albumartist or track.artist or "Unknown Artist",
        "album": track.album,
        "title": track.title or "Untitled",
        "track_no": track.track_no,
        "disc_no": track.disc_no,
        "year": track.year,
    }


def render(track: Track, template: str = DEFAULT_TEMPLATE) -> list[str]:
    """Applica il template e restituisce i segmenti di percorso già sanitizzati."""
    formatter = _TrackFormatter(_values_for(track))
    try:
        rendered = formatter.format(template)
    except KeyError as exc:
        raise ValueError(
            f"Placeholder sconosciuto {{{exc.args[0]}}} nel template di naming. "
            f"Validi: {', '.join(sorted(_ALLOWED_FIELDS))}"
        ) from None
    segments = []
    for raw in rendered.split(_SEPARATOR):
        raw = re.sub(r"^\s*-\s*", "", raw)  # "{track_no:02d} - {title}" senza numero -> "title"
        raw = re.sub(r"\s*-\s*$", "", raw)
        if not raw.strip():
            continue  # segmento vuoto (es. album sconosciuto): salta la sottocartella
        segments.append(sanitize(raw))
    if not segments:
        segments = [sanitize(track.title or "Untitled")]
    return segments


def build(track: Track, template: str, root: Path | str, ext: str | None = None) -> Path:
    """Percorso completo (senza estensione, o con ``ext``) sotto ``root``.

    Crea le directory intermedie. Non crea né sovrascrive il file.
    """
    root = Path(root)
    segments = render(track, template or DEFAULT_TEMPLATE)
    path = root.joinpath(*segments)
    if ext:
        path = path.with_name(path.name + (ext if ext.startswith(".") else f".{ext}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
