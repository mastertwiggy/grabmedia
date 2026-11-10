# Aggiornare yt-dlp

`yt-dlp` è pinnato esatto in `requirements.txt` come `yt-dlp[default]==<versione>`
(unico punto di pin per app e CLI). Due motivi:

- il core usa API interne di yt-dlp (`process_ie_result`, `js_runtimes`,
  `extract_cookies_from_browser`, i messaggi d'errore classificati da `ERROR_RULES`)
  e un aggiornamento può cambiarle in silenzio;
- l'extra `[default]` porta `yt-dlp-ejs`, gli script di risoluzione della signature,
  nella versione abbinata alla release. **Senza quel pacchetto deno da solo non
  basta**: yt-dlp trova il runtime ma emette "Signature solving failed" (visto
  nel primo run reale del job).

`requirements-dev.txt` e `requirements-cli.txt` dichiarano l'intervallo testato
(`>=2026.10.26,<2027`).

## Procedura automatica (job di bump)

Il workflow `.github/workflows/bump-ytdlp.yml` gira ogni lunedì e a richiesta
(`workflow_dispatch`). Input del dispatch: `version` (versione da provare, vuoto =
ultima su PyPI) e `force_checks` (esegue suite e canary anche se non c'è nessun
bump: serve a verificare il workflow stesso).

1. Prepara il runner: Python, deno (`denoland/setup-deno`), ffmpeg dalla build
   ufficiale `yt-dlp/FFmpeg-Builds` messa nel PATH. `ubuntu-latest` **non** ha
   ffmpeg preinstallato.
2. Legge il pin corrente e la versione candidata; **si ferma con errore esplicito**
   se la candidata esce dal range `<2027`: lì il pin va rivisto a mano.
3. Applica il bump in `requirements.txt`, installa `yt-dlp[default]==<versione>`,
   esegue la suite completa. Qualunque test saltato è un fallimento: il test
   d'integrazione `test_integration_process_ie_result_with_real_ytdlp` salta
   quando manca ffmpeg, e in CI non deve succedere.
4. Verifica il runtime JS: `deno --version` e la riga `JS runtimes:` dell'header
   verbose di yt-dlp (fallisce su `none` o riga assente).
5. Esegue il canary con rete reale (`GRABMEDIA_CANARY=1` esplicito; un canary
   saltato è un fallimento).
6. **Verde** → PR con il solo bump (se c'è) e chiusura delle issue `canary` aperte.
   **Rosso** → riepilogo nel job, annotazione d'errore e una sola issue con label
   `canary`: se ne esiste una aperta viene commentata con log e link al run,
   altrimenti viene creata (label compresa). Nessun merge.

Il reporting (riepilogo e issue) usa `if: always()`: gira anche quando uno step
iniziale fallisce in modo secco. C'è un solo job, quindi nessun `needs`.

### Il canary

`tests/test_canary_youtube.py`, logica pura in `evaluate()` coperta offline da
`tests/test_canary_logic.py`. In ordine di importanza:

1. **Gate primario**: nessuna riga di fallback sulla risoluzione della signature
   nell'output verbose (`SIGNATURE_FALLBACK_RE`).
2. Runtime rilevato dal core e da yt-dlp, nessun warning "No supported JavaScript runtime".
3. Formato scelto da `-f bestaudio`: solo 251 (opus) o 140 (m4a).
4. Pavimento di 100 kbps sulla mediana. È solo un pavimento contro il degrado, non
   una misura di qualità: opus 251 sta a ~127-129 kbps con o senza runtime.

**Verifica anti-bot di YouTube.** Sugli IP dei runner GitHub YouTube risponde a
volte "Sign in to confirm you're not a bot". Il canary non la aggira: il video
bloccato viene annotato (`NOTA:` nel log, warning nel run) e non valutato; se
sono bloccati tutti, il canary fallisce come **non conclusivo**. Un run verde con
un video annotato ha quindi verificato un solo video. Per un canary sempre
completo serve un runner con un IP non bloccato (self-hosted).

## Procedura manuale

```bash
# 1. versione candidata in un venv pulito (l'extra [default] porta yt-dlp-ejs)
python -m pip install -r requirements-dev.txt "yt-dlp[default]==<versione>"

# 2. suite completa offline + integrazione (serve ffmpeg nel PATH)
python -m pytest tests -q -rs

# 3. canary con rete reale (serve deno o node nel PATH)
GRABMEDIA_CANARY=1 python -m pytest tests/test_canary_youtube.py -q -rA

# 4. smoke reale della CLI
python -m core --dry-run "https://www.youtube.com/watch?v=jNQXAC9IVRw"
python -m core --out /tmp/gm --quality best "https://soundcloud.com/forss/flickermood"
```

Se tutto è verde: aggiorna `yt-dlp[default]==` in `requirements.txt` (e, solo se la
versione esce dal range, il limite superiore in `requirements-cli.txt` e
`requirements-dev.txt`), poi aggiorna questa pagina.

## Cosa guardare

- **`process_ie_result`** (`sources/_ytdlp.py`, `download_audio`): è API interna. Se il
  test d'integrazione fallisce, controllare firma e comportamento con un `info_dict`
  che contiene già `formats`.
- **`js_runtimes`**: il parametro è un dict `{"deno": {"path": ...}}`. Se cambia, cade
  `core/jsruntime.py` → `js_runtime_param`.
- **`yt-dlp-ejs`**: la versione richiesta è nei metadati di yt-dlp (extra `default`);
  `test_challenge_solver_scripts_are_installed` verifica che quella installata coincida.
- **Messaggi d'errore**: `ERROR_RULES` classifica a pattern. Nuovi messaggi finiscono
  come `unknown` (un retry, poi permanente) e vengono loggati a WARNING con il testo
  grezzo: dopo un bump guardare i log per aggiornare la tabella.
- **Cookie del browser**: `extract_cookies_from_browser` ha firma
  `(browser, profile, logger, *, keyring, container)`; la usa `browser_cookie_file`.
- **Estrattori**: SoundCloud (set in `extract_flat` senza titoli, `scsearch`),
  Bandcamp (fallback quando la pagina è una "Client Challenge"), YouTube
  (`ytsearch`, playlist album `OLAK5uy_`). Un cambio di shape degli `info_dict`
  si vede nei test di `tests/test_soundcloud_bandcamp.py` e `tests/test_youtube_generic.py`.
- **Deprecazioni**: yt-dlp avvisa a WARNING (dedup in `ExtractLogger`); leggerle.

## Lezioni dai primi run reali

Trovate solo eseguendo il workflow su GitHub, e ora coperte da
`test_workflow_lessons_from_the_first_real_runs`:

- senza `shell: bash` esplicito gli step non hanno `pipefail`: `ffmpeg -version | head -1`
  usciva con 0 anche senza ffmpeg;
- senza ffmpeg il test d'integrazione saltava in silenzio: ora uno skip in CI è un errore;
- un'action di terze parti per ffmpeg falliva nel download ("fetch failed");
- deno senza `yt-dlp-ejs` non risolve la signature.

## Tornare indietro

1. Riporta `yt-dlp[default]==<versione precedente>` in `requirements.txt` (la PR di
   bump contiene solo quella riga: basta un revert).
2. Reinstalla: `python -m pip install -r requirements.txt` (Docker: rebuild, il
   Dockerfile non fa più `--upgrade yt-dlp`).
3. Rilancia `python -m pytest tests -q` per confermare.
4. Se il job era rosso, la issue `canary` aperta contiene già log e link al run:
   aggiungi lì il contesto.

Il range `<2027` è un limite di prudenza, non una promessa di compatibilità:
alla prima release 2027 il job si ferma e qualcuno deve rifare i passi manuali.
