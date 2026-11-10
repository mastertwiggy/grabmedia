"""CLI del core.

    python -m core URL [URL ...] [--provider NOME] [--dry-run] [--quality Q]
                       [--out DIR] [--config FILE] [--overwrite] [-v]
    python -m core --list-providers
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from core import __version__, api as api_mod, config as config_mod, jsruntime, log as log_mod, registry
from core.pipeline import Pipeline, format_plan
from sources.base import ProviderError

log = logging.getLogger("grabmedia.cli")

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grabmedia",
        description="Scarica audio da URL tramite i provider in sources/.",
    )
    p.add_argument("urls", nargs="*", metavar="URL", help="una o più URL (traccia, album, playlist)")
    p.add_argument("--provider", metavar="NOME", help="forza un provider invece del dispatch automatico")
    p.add_argument("--dry-run", action="store_true", help="risolvi e mostra il piano senza scaricare")
    p.add_argument("--quality", metavar="Q", help="qualità per tutti i provider (override di [quality])")
    p.add_argument("--out", metavar="DIR", type=Path, help="cartella di output (override di general.out_dir)")
    p.add_argument("--config", metavar="FILE", type=Path, help="percorso di config.toml")
    p.add_argument("--env", metavar="FILE", type=Path, help="percorso del file .env (default: ./.env)")
    p.add_argument("--overwrite", action="store_true", help="riscarica anche le tracce già presenti")
    p.add_argument("--list-providers", action="store_true", help="elenca i provider e il loro stato")
    p.add_argument("--cookies", metavar="FILE", type=Path,
                   help="file di cookie in formato Netscape, passato a yt-dlp per tutti i provider")
    p.add_argument("--cookies-from-browser", metavar="BROWSER[+KEYRING][:PROFILO]",
                   help="cookie letti dal browser (sintassi di yt-dlp), per tutti i provider")
    p.add_argument("--renew-cookies", action="store_true",
                   help="scarta i cookie del browser già risolti in questa directory e rileggili alla prossima necessità")
    p.add_argument("--allow-no-js-runtime", action="store_true",
                   help="prosegui anche senza deno/node nel PATH (i siti che richiedono JavaScript possono perdere formati)")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v info dettagliate, -vv debug")
    p.add_argument("-q", "--quiet", action="store_true", help="solo warning ed errori")
    p.add_argument("--version", action="version", version=f"grabmedia core {__version__}")
    return p


def list_providers(order: list[str]) -> str:
    lines = ["Provider:"]
    loaded = registry.load_all(order)
    for source in loaded:
        kind = "audio" if source.supports_direct_audio else f"metadati -> {getattr(source, 'audio_provider', None) or 'auto'}"
        lines.append(f"  OK   {source.name:<14} {kind}")
    for name, reason in sorted(registry.UNAVAILABLE.items()):
        lines.append(f"  --   {name:<14} non disponibile: {reason}")
    if not loaded and not registry.UNAVAILABLE:
        lines.append("  (nessun modulo in sources/)")
    if order:
        lines.append(f"Priorità: {', '.join(order)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_mod.setup(args.verbose, args.quiet)

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        parser.exit(EXIT_USAGE, f"errore di configurazione: {exc}\n")
    config_mod.load_env(args.env)

    # Runtime JavaScript per yt-dlp: env YTDLP_JS_RUNTIME, poi deno, poi node.
    try:
        runtime = jsruntime.ensure(allow_missing=args.allow_no_js_runtime)
    except jsruntime.JsRuntimeMissing as exc:
        parser.exit(EXIT_USAGE, f"{exc}\n")
    if runtime is not None:
        log.debug("runtime JavaScript: %s", runtime.as_env())
    else:
        log.warning("Nessun runtime JavaScript: proseguo (--allow-no-js-runtime); i siti che richiedono JavaScript possono perdere formati")

    # Ciclo di vita dei cookie del browser: sweep degli orfani, pulizia sui segnali, rinnovo esplicito.
    removed = api_mod.prepare_cookie_store()
    if removed:
        log.info("Rimossi %d file cookie orfani", removed)
    if args.renew_cookies:
        api_mod.renew_browser_cookies()
        log.info("Cookie del browser scartati: verranno riletti alla prossima necessità")

    options = dict(cfg.options)
    defaults = dict(options.get("*", {}))
    if args.cookies:
        if not args.cookies.is_file():
            parser.exit(EXIT_USAGE, f"--cookies: file non trovato: {args.cookies}\n")
        defaults["cookiefile"] = str(args.cookies.resolve())
    if args.cookies_from_browser:
        defaults["cookiesfrombrowser"] = args.cookies_from_browser
    if defaults:
        options["*"] = defaults
    registry.configure(options, cfg.priority)

    if args.out:
        cfg.out_dir = args.out.expanduser().resolve()
    if args.quality:
        cfg.quality_default = args.quality
        cfg.quality.clear()

    if args.list_providers:
        print(list_providers(cfg.priority))
        return EXIT_OK

    if not args.urls:
        parser.error("indica almeno una URL (oppure --list-providers)")

    pipeline = Pipeline(
        cfg,
        dry_run=args.dry_run,
        forced_provider=args.provider,
        overwrite=True if args.overwrite else None,
    )
    try:
        summary = pipeline.run(args.urls)
    except ProviderError as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    except KeyboardInterrupt:
        log.warning("Interrotto dall'utente")
        return EXIT_PARTIAL
    finally:
        pipeline.close()

    print(format_plan(summary, dry_run=args.dry_run))
    return EXIT_OK if summary.ok else EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
