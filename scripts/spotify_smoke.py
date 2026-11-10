"""Smoke test REALE del provider Spotify e confronto con le fixture dei test.

Uso (servono SPOTIFY_CLIENT_ID e SPOTIFY_CLIENT_SECRET in .env):

    python scripts/spotify_smoke.py "https://open.spotify.com/playlist/<id>"

Cosa fa:
1. risolve la playlist con il provider vero, registrando le risposte dell'API
   in una cassetta vcrpy (token e header di autorizzazione oscurati);
2. confronta CAMPO PER CAMPO la forma delle risposte reali (item di playlist,
   traccia, album, immagine, artista, pagina) con quella delle fixture
   sintetiche usate dai test offline (``tests/test_spotify.py``);
3. stampa le divergenze. Quelle "critiche" (un campo che il provider legge
   manca o ha un altro tipo nella risposta reale) fanno uscire con codice 1:
   lì vanno corrette le fixture e, se serve, il provider.

La logica di confronto (``shape``, ``compare``) è pura e coperta da
``tests/test_spotify_smoke_tool.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CASSETTE = ROOT / "tests" / "cassettes" / "spotify_playlist_real.yaml"

# Campi che sources/spotify.py legge davvero: se mancano o cambiano tipo nella
# risposta reale è una divergenza critica.
USED_FIELDS: dict[str, tuple[str, ...]] = {
    "page": ("items", "next", "total"),
    "item": ("is_local",),
    "track": ("id", "name", "uri", "type", "artists", "duration_ms", "track_number", "disc_number",
              "explicit", "is_local", "album"),
    "album": ("name", "release_date", "total_tracks", "artists", "images"),
    "image": ("url", "width", "height"),
    "artist": ("name",),
}


def shape(obj: Any) -> dict[str, str]:
    """Forma di un oggetto JSON a un livello: ``{campo: tipo}`` (i ``None`` come 'null')."""
    if not isinstance(obj, dict):
        return {}
    return {key: _type_name(value) for key, value in obj.items()}


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def compare(kind: str, real: dict[str, str], fixture: dict[str, str]) -> dict[str, list[str]]:
    """Confronto campo per campo tra forma reale e forma della fixture.

    * ``critical``: un campo usato dal provider manca nella risposta reale, o
      ha un tipo diverso da quello della fixture (``null`` è sempre tollerato);
    * ``fixture_only``: la fixture ha un campo che l'API reale non restituisce;
    * ``missing_in_fixture``: campo usato dal provider, presente nel reale ma
      assente dalla fixture (i test non lo esercitano);
    * ``real_only``: campi reali che la fixture non modella (informativo).
    """
    used = USED_FIELDS.get(kind, ())
    out: dict[str, list[str]] = {"critical": [], "fixture_only": [], "missing_in_fixture": [], "real_only": []}
    for name in used:
        if name not in real:
            out["critical"].append(f"{kind}.{name}: usato dal provider ma assente nella risposta reale")
        elif name not in fixture:
            out["missing_in_fixture"].append(f"{kind}.{name}: presente nel reale ({real[name]}), assente dalla fixture")
        elif real[name] != fixture[name] and "null" not in (real[name], fixture[name]):
            out["critical"].append(f"{kind}.{name}: tipo reale {real[name]} != fixture {fixture[name]}")
    for name in fixture:
        if name not in real:
            out["fixture_only"].append(f"{kind}.{name}: nella fixture ({fixture[name]}) ma non nella risposta reale")
    out["real_only"] = sorted(f"{kind}.{name}" for name in real if name not in fixture)
    return out


def merge(reports: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    total: dict[str, list[str]] = {"critical": [], "fixture_only": [], "missing_in_fixture": [], "real_only": []}
    for report in reports:
        for key, values in report.items():
            total[key].extend(v for v in values if v not in total[key])
    return total


def shapes_from_page(page: dict) -> dict[str, dict[str, str]]:
    """Estrae le forme (page, item, track, album, image, artist) da una pagina di playlist."""
    shapes: dict[str, dict[str, str]] = {"page": shape(page)}
    items = [i for i in (page.get("items") or []) if i]
    if not items:
        return shapes
    item = items[0]
    shapes["item"] = shape(item)
    track = item.get("track") or item.get("item") or {}
    shapes["track"] = shape(track)
    album = track.get("album") or {}
    shapes["album"] = shape(album)
    images = album.get("images") or []
    if images:
        shapes["image"] = shape(images[0])
    artists = track.get("artists") or []
    if artists:
        shapes["artist"] = shape(artists[0])
    return shapes


def fixture_shapes() -> dict[str, dict[str, str]]:
    """Forme delle fixture sintetiche dei test offline."""
    from tests import test_spotify as fx

    album = fx.album_obj(3)
    track = fx.track_obj(1, album=album)
    page = fx.page([{"is_local": False, "track": track}], 0, 100, 1)
    return shapes_from_page(page)


def _scrub_response(response: dict) -> dict:
    """Oscura l'access token nelle risposte registrate."""
    try:
        body = response["body"]["string"]
        text = body.decode("utf-8") if isinstance(body, bytes) else body
        data = json.loads(text)
    except (KeyError, ValueError, AttributeError, UnicodeDecodeError):
        return response
    if isinstance(data, dict) and "access_token" in data:
        data["access_token"] = "REDACTED"
        scrubbed = json.dumps(data)
        response["body"]["string"] = scrubbed.encode("utf-8") if isinstance(body, bytes) else scrubbed
    return response


def real_playlist_pages(cassette_path: Path) -> list[dict]:
    """Risposte JSON delle pagine di playlist registrate nella cassetta."""
    import yaml

    data = yaml.safe_load(cassette_path.read_text(encoding="utf-8")) or {}
    pages: list[dict] = []
    for interaction in data.get("interactions", []):
        uri = interaction.get("request", {}).get("uri", "")
        if "/playlists/" not in uri or not uri.split("?")[0].rstrip("/").endswith(("/items", "/tracks")):
            continue
        body = interaction.get("response", {}).get("body", {}).get("string", "")
        try:
            pages.append(json.loads(body.decode("utf-8") if isinstance(body, bytes) else body))
        except ValueError:
            continue
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test reale del provider Spotify + confronto con le fixture")
    parser.add_argument("url", help="URL di una playlist pubblica (idealmente 3 brani)")
    parser.add_argument("--cassette", type=Path, default=DEFAULT_CASSETTE, help="dove registrare la cassetta vcrpy")
    parser.add_argument("--market", default=None, help="market ISO (default: nessuno)")
    args = parser.parse_args(argv)

    from core import config as config_mod

    config_mod.load_env()
    if not os.environ.get("SPOTIFY_CLIENT_ID") or not os.environ.get("SPOTIFY_CLIENT_SECRET"):
        print("Mancano SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET: copia .env.example in .env e compila "
              "(https://developer.spotify.com/dashboard).", file=sys.stderr)
        return 2

    import vcr

    from sources.base import ProviderError
    from sources.spotify import SpotifySource

    args.cassette.parent.mkdir(parents=True, exist_ok=True)
    if args.cassette.exists():
        args.cassette.unlink()
    recorder = vcr.VCR(record_mode="all", filter_headers=[("authorization", "REDACTED")],
                       before_record_response=_scrub_response, decode_compressed_response=True)
    options: dict[str, Any] = {"cache_path": str(Path(tempfile.mkdtemp(prefix="gm-smoke-")) / "cache.sqlite3")}
    if args.market:
        options["market"] = args.market
    try:
        with recorder.use_cassette(str(args.cassette)):
            source = SpotifySource(options)
            tracks = source.resolve(args.url)
            source.close()
    except ProviderError as exc:
        print(f"Resolve fallito: {exc}", file=sys.stderr)
        return 1

    print(f"Risolte {len(tracks)} tracce da {args.url}:")
    for t in tracks:
        print(f"  - {t.display()} | album={t.album!r} year={t.year} track_no={t.track_no} disc_no={t.disc_no} "
              f"duration_ms={t.duration_ms} cover={'sì' if t.cover_url else 'no'} isrc={t.isrc}")

    pages = real_playlist_pages(args.cassette)
    if not pages:
        print("Nessuna pagina di playlist nella cassetta: impossibile confrontare.", file=sys.stderr)
        return 1
    fixtures = fixture_shapes()
    real = shapes_from_page(pages[0])
    report = merge([compare(kind, real.get(kind, {}), fixtures.get(kind, {})) for kind in USED_FIELDS])
    print(f"\nCassetta: {args.cassette}")
    labels = {
        "critical": "DIVERGENZE CRITICHE (campo usato dal provider assente o di tipo diverso)",
        "fixture_only": "Solo nella fixture (l'API reale non lo restituisce)",
        "missing_in_fixture": "Usato dal provider ma non modellato dalla fixture",
        "real_only": "Solo nel reale (informativo)",
    }
    for key, label in labels.items():
        print(f"\n{label}: {len(report[key])}")
        for line in report[key]:
            print(f"  - {line}")
    return 1 if report["critical"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
