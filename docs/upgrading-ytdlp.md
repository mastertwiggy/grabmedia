# Aggiornare yt-dlp

`yt-dlp` è pinnato esatto in `requirements.txt` (unico punto di pin per app e CLI).
Il motivo: il core usa API interne di yt-dlp (`process_ie_result`, `js_runtimes`,
`cookiesfrombrowser`, i messaggi d'errore classificati da `ERROR_RULES`) e un
aggiornamento può cambiarle in silenzio. `requirements-dev.txt` e
`requirements-cli.txt` dichiarano l'intervallo testato (`>=2026.10.26,<2027`).

## Procedura automatica (job di bump)

Il workflow `.github/workflows/bump-ytdlp.yml` gira ogni lunedì e a richiesta
(`workflow_dispatch`, con una versione opzionale):

1. legge il pin corrente e l'ultima release su PyPI;
2. **si ferma con errore esplicito** se l'ultima release esce dal range `<2027`:
   lì il pin va rivisto a mano (sezione sotto), non bumpato;
3. applica il bump in `requirements.txt`, installa, esegue la suite completa
   (incluso il test d'integrazione `test_integration_process_ie_result_with_real_ytdlp`)
   e il canary con rete reale `tests/test_canary_youtube.py`;
4. verde → apre una PR con il solo bump; rosso → apre una issue con la coda dei
   log e non mergia niente.

## Procedura manuale

```bash
# 1. versione candidata in un venv pulito
python -m pip install -r requirements-dev.txt "yt-dlp==<versione>"

# 2. suite completa offline + integrazione
python -m pytest tests -q

# 3. canary con rete reale (serve un runtime JS: deno o node nel PATH)
GRABMEDIA_CANARY=1 python -m pytest tests/test_canary_youtube.py -q -rA

# 4. smoke reale della CLI
python -m core --dry-run "https://www.youtube.com/watch?v=jNQXAC9IVRw"
python -m core --out /tmp/gm --quality best "https://soundcloud.com/forss/flickermood"
```

Se tutto è verde: aggiorna `yt-dlp==` in `requirements.txt` (e, solo se la
versione esce dal range, il limite superiore in `requirements-cli.txt` e
`requirements-dev.txt`), poi aggiorna questa pagina.

## Cosa guardare

- **`process_ie_result`** (`sources/_ytdlp.py`, `download_audio`): è API interna. Se il
  test d'integrazione fallisce, controllare firma e comportamento con un `info_dict`
  che contiene già `formats`.
- **`js_runtimes`**: il parametro è un dict `{"deno": {"path": ...}}`. Se cambia, cade
  `core/jsruntime.py` → `js_runtime_param`. Il canary (`tests/test_canary_youtube.py`)
  ha come gate primario le righe di fallback sulla risoluzione della signature
  (`SIGNATURE_FALLBACK_RE`, con `verbose`), poi il runtime rilevato (step del
  workflow su `deno --version` e sulla riga `JS runtimes:` di yt-dlp, più le
  stesse asserzioni nel test), l'id del formato scelto (solo 251 o 140) e un
  pavimento di 100 kbps sulla mediana di due video. Il bitrate è solo un
  pavimento contro il degrado, non una misura di qualità: misurato il
  2026-11-06, senza runtime `-f bestaudio` dà comunque opus 251 a ~127-129 kbps.
- **Issue `canary`**: su run rosso il workflow commenta la issue aperta con label
  `canary` (o la crea, label compresa) con log e link al run; su run verde chiude
  quelle aperte. Una sola issue aperta alla volta.
- **Messaggi d'errore**: `ERROR_RULES` classifica a pattern. Nuovi messaggi finiscono
  come `unknown` (un retry, poi permanente) e vengono loggati a WARNING con il testo
  grezzo: dopo un bump guardare i log per aggiornare la tabella.
- **`cookiesfrombrowser`**: tupla `(browser, profile, keyring, container)` prodotta da
  `parse_browser_spec`; `extract_cookies_from_browser` ha firma
  `(browser, profile, logger, *, keyring, container)`.
- **Estrattori**: SoundCloud (set in `extract_flat` senza titoli, `scsearch`),
  Bandcamp (fallback quando la pagina è una "Client Challenge"), YouTube
  (`ytsearch`, playlist album `OLAK5uy_`). Un cambio di shape degli `info_dict`
  si vede nei test di `tests/test_soundcloud_bandcamp.py` e `tests/test_youtube_generic.py`.
- **Deprecazioni**: yt-dlp avvisa a WARNING (dedup in `ExtractLogger`); leggerle.

## Tornare indietro

1. Riporta `yt-dlp==<versione precedente>` in `requirements.txt` (la PR di bump
   contiene solo quella riga: basta un revert).
2. Reinstalla: `python -m pip install -r requirements.txt` (Docker: rebuild, il
   Dockerfile non fa più `--upgrade yt-dlp`).
3. Rilancia `python -m pytest tests -q` per confermare.
4. Apri una issue con l'output del canary o della suite, così il prossimo bump
   automatico ha il contesto.

Il range `<2027` è un limite di prudenza, non una promessa di compatibilità:
alla prima release 2027 il job si ferma e qualcuno deve rifare i passi manuali.
