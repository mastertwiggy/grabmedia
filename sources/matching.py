"""Matcher puro tra una traccia cercata e i risultati di una ricerca.

Nessun side effect, nessuna dipendenza da yt-dlp: ``pick`` riceve dizionari
(``id``, ``title``, ``duration`` in secondi, ``uploader``/``channel``) e
restituisce il migliore con il punteggio, oppure ``(None, score)`` se nessun
candidato supera la soglia.

Punteggio = similarità del titolo (SequenceMatcher) + bonus artista (più alto
se combacia con l'uploader che se compare solo nel titolo) + bonus/penalità
durata - penalità per versioni indesiderate (cover, live, remix, sped up,
slowed, nightcore, 8d, karaoke, instrumental, quando la query non le chiede)
- penalità per canali aggregatori.

La soglia da sola non basta: due candidati entro ``min_margin`` sarebbero
decisi dall'ordine di ritorno del sito. La classifica è quindi
deterministica: punteggio, poi uploader che combacia con l'artista, poi
durata più vicina, poi ordine di arrivo.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

__provider__ = False  # helper puro: il registry non deve trattarlo come provider

log = logging.getLogger("grabmedia.matching")

DEFAULT_THRESHOLD = 0.72
MIN_MARGIN = 0.03

ARTIST_IN_UPLOADER_BONUS = 0.12
ARTIST_IN_TITLE_BONUS = 0.06
ARTIST_MISSING_PENALTY = 0.35
DURATION_CLOSE_MS = 2000
DURATION_CLOSE_BONUS = 0.05
DURATION_TOLERANCE_MS = 5000
DURATION_PENALTY_PER_SECOND = 0.03
DURATION_PENALTY_CAP = 0.60
BAD_VERSION_PENALTY = 0.50
AGGREGATOR_PENALTY = 0.15

# Versioni da evitare quando non sono richieste dalla query stessa.
BAD_VERSION_TERMS: tuple[str, ...] = (
    "cover", "live", "remix", "sped up", "speed up", "nightcore", "8d", "karaoke",
    "instrumental", "slowed", "reverb", "bootleg", "mashup",
)
# Canali che ripubblicano testi/mix invece dell'audio ufficiale.
AGGREGATOR_TERMS: tuple[str, ...] = (
    "lyrics", "lyric", "letra", "letras", "testi", "testo", "karaoke",
    "nightcore", "8d audio", "bass boosted", "mix", "hits", "tunes", "vibes",
    "chill", "sound", "sounds", "records tv",
)

# Rumore nei titoli dei video che non fa parte del titolo della canzone.
_NOISE_PATTERNS = [
    r"\(.*?\)", r"\[.*?\]", r"\{.*?\}",
    r"\bofficial\s+(music\s+)?video\b", r"\bofficial\s+audio\b", r"\bofficial\b",
    r"\blyric\s+video\b", r"\blyrics?\b", r"\baudio\b", r"\bvideo\b",
    r"\bvisuali[sz]er\b", r"\bhd\b", r"\bhq\b", r"\b4k\b", r"\bmv\b",
    r"\bfeat\.?\s+.*$", r"\bft\.?\s+.*$", r"\bfeaturing\s+.*$",
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """lowercase, NFC, senza feat./official video/lyrics/audio, parentesi e bracket."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text)).lower()
    text = _NOISE_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip()


def _plain(text: str | None) -> str:
    """lowercase + NFC + punteggiatura rimossa, ma SENZA togliere parentesi:
    serve per cercare 'live', 'cover', ... anche dentro le parentesi."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text)).lower()
    text = _PUNCT_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip()


def _contains_term(haystack: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack) is not None


def _artist_present(artist_plain: str, haystack: str) -> bool:
    """Artista presente come parole intere, oppure come forma "schiacciata"
    (``daftpunk`` in ``daftpunkvevo``) quando è abbastanza lunga."""
    if not artist_plain or not haystack:
        return False
    if _contains_term(haystack, artist_plain):
        return True
    squashed = artist_plain.replace(" ", "")
    return len(squashed) >= 4 and squashed in haystack.replace(" ", "")


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _candidate_duration_ms(candidate: Mapping[str, Any]) -> int | None:
    for key in ("duration_ms", "duration"):
        value = candidate.get(key)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        return int(value) if key == "duration_ms" else int(value * 1000)
    return None


def _uploader(candidate: Mapping[str, Any]) -> str:
    for key in ("uploader", "channel", "artist", "creator"):
        value = candidate.get(key)
        if value:
            return str(value)
    return ""


def uploader_matches(candidate: Mapping[str, Any], artist: str | None) -> bool:
    return _artist_present(_plain(artist), _plain(_uploader(candidate)))


def duration_delta_ms(candidate: Mapping[str, Any], want_duration_ms: int | None) -> int | None:
    if want_duration_ms is None:
        return None
    c_dur = _candidate_duration_ms(candidate)
    return abs(c_dur - int(want_duration_ms)) if c_dur is not None else None


def score(
    query: str,
    candidate: Mapping[str, Any],
    want_duration_ms: int | None = None,
    *,
    artist: str | None = None,
    title: str | None = None,
) -> float:
    """Punteggio grezzo di un singolo candidato (non limitato a [0, 1]: i bonus
    devono poter distinguere due candidati altrimenti identici)."""
    q_title = normalize(title or query)
    q_full = normalize(query)
    q_plain = _plain(query) + " " + _plain(title)
    c_title = normalize(candidate.get("title"))
    c_plain = _plain(candidate.get("title"))
    uploader_plain = _plain(_uploader(candidate))

    value = max(similarity(q_title, c_title), similarity(q_full, c_title))

    artist_norm = _plain(artist)
    if artist_norm:
        if _artist_present(artist_norm, uploader_plain):
            value += ARTIST_IN_UPLOADER_BONUS
        elif _artist_present(artist_norm, c_plain):
            value += ARTIST_IN_TITLE_BONUS
        else:
            value -= ARTIST_MISSING_PENALTY

    delta = duration_delta_ms(candidate, want_duration_ms)
    if delta is not None:
        if delta < DURATION_CLOSE_MS:
            value += DURATION_CLOSE_BONUS
        elif delta > DURATION_TOLERANCE_MS:
            over_s = (delta - DURATION_TOLERANCE_MS) / 1000
            value -= min(DURATION_PENALTY_CAP, over_s * DURATION_PENALTY_PER_SECOND)

    for term in BAD_VERSION_TERMS:
        if _contains_term(c_plain, term) and not _contains_term(q_plain, term):
            value -= BAD_VERSION_PENALTY
            break

    if uploader_plain and not (artist_norm and _artist_present(artist_norm, uploader_plain)):
        if any(_contains_term(uploader_plain, term) for term in AGGREGATOR_TERMS):
            value -= AGGREGATOR_PENALTY

    return value


def pick(
    query: str,
    candidates: Sequence[Mapping[str, Any]] | None,
    want_duration_ms: int | None = None,
    *,
    artist: str | None = None,
    title: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    min_margin: float = MIN_MARGIN,
) -> tuple[dict[str, Any] | None, float]:
    """Miglior candidato e punteggio (limitato a [0, 1]); ``(None, migliore)``
    sotto soglia.

    ``query`` è la stringa cercata (di solito "artista titolo"); ``artist`` e
    ``title`` separati migliorano il confronto quando sono noti. Con
    ``want_duration_ms=None`` la durata non viene confrontata. Se i primi due
    sono entro ``min_margin`` decidono, nell'ordine, uploader = artista, durata
    più vicina, ordine di arrivo: mai l'ordine casuale del sito da solo.
    """
    ranked: list[tuple[float, bool, float, int, Mapping[str, Any]]] = []
    for index, candidate in enumerate(candidates or ()):
        if not candidate or not candidate.get("title"):
            continue
        value = score(query, candidate, want_duration_ms, artist=artist, title=title)
        up = uploader_matches(candidate, artist) if artist else False
        delta = duration_delta_ms(candidate, want_duration_ms)
        ranked.append((value, up, float("inf") if delta is None else float(delta), index, candidate))
    if not ranked:
        return None, 0.0
    ranked.sort(key=lambda r: (-r[0], not r[1], r[2], r[3]))
    best = ranked[0]
    if len(ranked) > 1 and best[0] - ranked[1][0] < min_margin:
        second = ranked[1]
        reason = ("uploader = artista" if best[1] != second[1] else
                  "durata più vicina" if best[2] != second[2] else "ordine di arrivo")
        log.debug("match ambiguo per '%s' (%.3f vs %.3f): scelto '%s' per %s",
                  query, best[0], second[0], best[4].get("title"), reason)
    reported = max(0.0, min(1.0, best[0]))
    if best[0] < threshold:
        return None, reported
    return dict(best[4]), reported
