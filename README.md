# GrabMedia

Servizio di download video/audio (YouTube, SoundCloud, Vimeo e tutti i siti supportati da yt-dlp).
FastAPI + RQ + Redis + nginx, pronto per `docker compose`.

## Programma desktop (Windows e macOS)

Lo stesso sito in un programma da doppio clic, senza installare niente: dentro
ci sono Python, yt-dlp, ffmpeg e deno. Funziona su qualunque PC o Mac, anche
senza Docker, Redis o nginx.

**Uso**

- **Windows**: estrai `GrabMedia-windows-x64.zip` (tutta la cartella) e apri
  `GrabMedia.exe`. Al primo avvio Windows può mostrare "PC protetto da Windows":
  *Ulteriori informazioni* → *Esegui comunque* (il programma non è firmato).
- **macOS**: estrai `GrabMedia-macos-arm64.zip` (Mac con Apple Silicon) oppure
  `GrabMedia-macos-x64.zip` (Mac Intel) e apri `GrabMedia.app`. Il programma non
  è firmato: al primo avvio macOS lo blocca; vai in *Impostazioni di Sistema →
  Privacy e sicurezza* e premi *Apri comunque* (su macOS 14 o precedenti basta
  clic destro → *Apri*).

Si apre una finestrella con tre pulsanti (*Apri GrabMedia*, *Apri la cartella
della musica*, *Esci*) e il sito nel browser. La finestra va lasciata aperta
mentre si scarica; *Esci* ferma tutto.

- Nessuna password: il programma ascolta solo su `127.0.0.1` (non è raggiungibile
  da altri computer), quindi il riquadro di accesso non compare.
- La musica di "Provider audio" va in `Musica/GrabMedia`; i download classici si
  salvano dal browser come sempre.
- "Provider audio" scarica **sempre in MP3** (192 kbps se non si sceglie altro):
  attivandolo la pagina passa da sola a *Solo audio MP3*. Il formato originale
  (`best`, spesso `.opus`) resta disponibile dalla CLI e da `config.toml`.
- Configurazione, stato e log stanno nella cartella dati dell'utente
  (`%LOCALAPPDATA%\GrabMedia` su Windows, `~/Library/Application Support/GrabMedia`
  su macOS): `config.toml` si può modificare, `grabmedia.log` serve se qualcosa non va.

**Costruirlo**

```bash
pip install -r requirements-desktop.txt
python desktop/build.py
```

Il risultato è in `desktop/dist` (cartella `GrabMedia/` + zip su Windows,
`GrabMedia.app` + zip su macOS). Serve deno installato sulla macchina che
costruisce (viene copiato nel pacchetto); ffmpeg arriva da `imageio-ffmpeg`.
PyInstaller non compila per un altro sistema: la versione Mac si costruisce su
un Mac. Per questo c'è il workflow `.github/workflows/build-desktop.yml`
(*Programma desktop*), manuale o al push di un tag `v*`: costruisce Windows,
macOS Apple Silicon e macOS Intel, **prova ogni pacchetto sul runner**
(`--self-check` e avvio reale con `/health`) e carica gli zip come artifact; con
un tag, o con l'input `release_tag`, li pubblica anche in una Release del repository.

Modalità dell'eseguibile, utili per le verifiche: `--self-check` (controlla che
il pacchetto sia completo, exit 0/1), `--no-gui --port N` (solo server).
Limiti noti: il programma non è firmato né notarizzato; lo zip pesa circa 110 MB
perché contiene ffmpeg e deno.

## Avvio

```bash
cp .env.example .env
cp nginx/.htpasswd.example nginx/.htpasswd
docker compose up -d --build
```

Interfaccia web: http://localhost:8080

Documentazione API (Swagger): http://localhost:8080/docs

## Credenziali per il download dei file

Il download dei file (`GET /download/{job_id}/{filename}`) è protetto da Basic Auth in nginx.
Le credenziali stanno in `nginx/.htpasswd`, che **non è nel repository** (è in
`.gitignore`): si crea copiando il modello `nginx/.htpasswd.example`, che contiene
quelle di esempio:

- utente: `grabmedia`
- password: `grabmedia2026`

**Sono pubbliche: vanno cambiate prima di esporre il sito in rete.** Per cambiarle
(richiede `apache2-utils` / `httpd-tools`, oppure usa Docker):

```bash
docker run --rm httpd:2.4-alpine htpasswd -nbB grabmedia NUOVA_PASSWORD > nginx/.htpasswd
```

Poi `docker compose restart nginx`.

## API

| Metodo | Endpoint                          | Descrizione                                   |
|--------|-----------------------------------|-----------------------------------------------|
| POST   | `/download`                       | `{"url": "...", "format": "best"}` → `{"job_id"}` |
| GET    | `/status/{job_id}`                | Stato, progresso, errore, link download        |
| WS     | `/ws/{job_id}`                    | Aggiornamenti in tempo reale                   |
| GET    | `/download/{job_id}/{filename}`   | Scarica il file (Basic Auth)                   |
| GET    | `/jobs`                           | Ultimi 10 job                                  |
| POST   | `/cleanup`                        | Cancella job e file più vecchi di 24 ore       |
| GET    | `/health`                         | Stato del servizio                             |

Formati: `best`, `1080p`, `720p`, `4K`, `solo audio (mp3)` (accettati anche `mp3`, `audio`, `4k`, `2160p`).

Per l'mp3 si può indicare il bitrate con `audio_bitrate` (128, 192, 256 o 320 kbps; default 192):

```json
{"url": "https://www.youtube.com/watch?v=...", "format": "mp3", "audio_bitrate": 320}
```

## Variabili d'ambiente

| Variabile                  | Default          | Descrizione                                  |
|----------------------------|------------------|----------------------------------------------|
| `REDIS_HOST` / `REDIS_PORT`| `localhost`/`6379` | Connessione Redis                           |
| `DOWNLOAD_DIR`             | `/data/downloads` | Cartella dei file scaricati                 |
| `MAX_DOWNLOAD_SIZE_MB`     | `500`            | Dimensione massima per download              |
| `FILE_TTL_HOURS`           | `24`             | Ore dopo le quali i file vengono cancellati  |
| `JOB_TIMEOUT_SECONDS`      | `1800`           | Timeout di un singolo download               |
| `CLEANUP_INTERVAL_SECONDS` | `3600`           | Frequenza della pulizia automatica (worker)  |
| `BLOCK_PRIVATE_HOSTS`      | `1`              | Rifiuta URL verso host privati/locali        |
| `HTTP_PORT`                | `8080`           | Porta pubblica di nginx                      |

## Avvio rapido in locale senza Docker (Windows/Mac/Linux)

Servono Python 3.11+ e ffmpeg (su Windows: `winget install Gyan.FFmpeg`).
Redis non serve: lo script usa un Redis in memoria (fakeredis), oppure quello vero se è già in ascolto.

```bash
pip install -r requirements-dev.txt
```

```bash
python run_dev.py
```

Poi apri http://127.0.0.1:8000. I file finiscono in `downloads/`.
In questa modalità i job si perdono al riavvio (Redis in memoria) e i file non sono protetti da Basic Auth (manca nginx).

## Sviluppo locale manuale

Servono Python 3.11+, ffmpeg e un Redis in ascolto su localhost:6379.

```bash
pip install -r requirements.txt
```

```bash
uvicorn app.main:app --reload --port 8000
```

```bash
python -m app.worker
```

Il frontend è servito anche da FastAPI su http://localhost:8000 (senza Basic Auth sui file).

## CLI a provider (core/ + sources/)

Accanto alla web app c'è un core a riga di comando indipendente dai servizi:

- `sources/base.py`: contratto comune (`Track`, `AudioSource`, `MetadataSource`, `ProviderError`, `ProviderUnavailable`).
- `core/registry.py`: scopre i provider in `sources/<nome>.py` in modo lazy; un modulo rotto finisce in `UNAVAILABLE` e non blocca gli altri.
- `core/naming.py`: percorso di destinazione da template (`{artist}/{album}/{track_no:02d} - {title}`), sanitizzato e normalizzato NFC.
- `core/state.py`: stato per traccia su SQLite (`pending`, `running`, `done`, `failed`, `skipped`) con hash del match per il resume.
- `core/retry.py`: backoff esponenziale con jitter su 429/5xx e circuit breaker (5 errori consecutivi).
- `core/concurrency.py`: semaforo per provider; `core/pipeline.py`: orchestrazione; `core/cli.py`: comando.

Configurazione in `config.toml` (`out_dir`, `naming`, `overwrite`, `priority`, `concurrency`, `quality`, `retry`); le credenziali solo in `.env` (vedi `.env.example`).

```bash
python -m core --list-providers
python -m core --dry-run "https://..."
python -m core --provider <nome> --quality 320k "https://..."
```

Installazione della CLI: `pip install -r requirements-cli.txt` (stesso runtime della web app). Servono ffmpeg nel PATH e, per YouTube, un runtime JavaScript (deno o node); senza, la CLI esce con le istruzioni di installazione, salvo `--allow-no-js-runtime`.

Test (senza rete): `pip install -r requirements-dev.txt` e poi `python -m pytest tests -q`. Il test d'integrazione salta se manca ffmpeg; il canary YouTube gira solo con `GRABMEDIA_CANARY=1`.

### Provider disponibili

| Provider | URL | Delega (ricerca) | Note |
|---|---|---|---|
| `youtube` | youtube.com, youtu.be, music.youtube.com (video, playlist, album, canali) | sì: cerca "artista titolo" e sceglie con `sources/matching.py` | richiede ffmpeg, un runtime JS (deno) e `yt-dlp-ejs`, che arriva con `yt-dlp[default]` |
| `generic` | qualsiasi http/https che nessun altro provider rivendica | no | passthrough yt-dlp, mai scelto come `audio_provider` |

Qualità (`[quality]` o `--quality`): `best` (default: formato nativo, nessuna transcodifica), `m4a`, `mp3`, `mp3_320`, `320k`, `opus`, `vorbis`, `flac`, `wav`, `alac`. Vedi "Qualità e formati".
Opzioni per provider in `[options.youtube]`: `cookiefile`, `ffmpeg_location`, `match_threshold` (default 0.72), `search_results` (default 5).
Se ffmpeg non è nel PATH puoi anche esportare `FFMPEG_LOCATION`.

Una traccia delegata senza match affidabile viene marcata `skipped` (non `failed`) con la query nel messaggio.

### Runtime JavaScript (deno): richiesto per YouTube nel Provider audio

yt-dlp ha bisogno di un runtime JavaScript per risolvere le protezioni di YouTube. **Senza deno il provider `youtube` del Provider audio è disabilitato**: i link YouTube e tutti i link Spotify (il cui audio viene cercato su YouTube) falliscono con un messaggio che riporta queste istruzioni. I download normali del sito non ne risentono.

```bash
winget install DenoLand.Deno
```

```bash
brew install deno
```

```bash
curl -fsSL https://deno.land/install.sh | sh
```

Dopo l'installazione riapri il terminale (o riavvia l'app) perché `deno` entri nel PATH. In alternativa indica il binario con `YTDLP_JS_RUNTIME=deno:/percorso/deno`; va bene anche `node`. Servono anche gli script `yt-dlp-ejs`, che arrivano con `yt-dlp[default]` (già in `requirements.txt`). Nella CLI `--allow-no-js-runtime` prosegue comunque, con il rischio di formati mancanti. L'immagine Docker non include ancora deno.

### Spotify

I link Spotify danno solo metadati: l'audio viene cercato su un altro provider (`audio_provider`, di default YouTube). Non si scarica mai audio da Spotify. Ci sono due modi per leggere i metadati.

**1. Pagina embed pubblica (default, nessuna credenziale)**: `sources/spotify_embed.py` scarica `https://open.spotify.com/embed/{tipo}/{id}`, legge il JSON `__NEXT_DATA__` e mappa `trackList` in tracce. Copre track, album e playlist.

- Disponibili: titolo, artisti, durata; per gli album anche nome, artista dell'album, numero di traccia e copertina; per la traccia singola anche anno e copertina.
- Non disponibili: ISRC, numero di disco, album e copertina dei brani di una playlist (l'immagine della playlist finisce in `extra`, non viene spacciata per copertina del brano), data degli album.
- Se Spotify cambia la pagina e `__NEXT_DATA__` sparisce, l'errore lo dice e indica l'alternativa qui sotto.

**2. Web API ufficiale (spotipy), solo su richiesta esplicita**: `sources/spotify.py`. Richiede credenziali in `.env` **e Spotify Premium sull'account proprietario dell'app**: senza Premium la Web API risponde 403 "Active premium subscription required for the owner of the app" a ogni richiesta. Si usa in tre casi: forzandola con `--provider spotify`, per gli URL artista (la pagina embed non li copre), oppure come ripiego quando la pagina embed non è leggibile, ma solo se `mode = "spotify_api"` è impostato in `[options.spotify_embed]` e le credenziali sono in `.env`.

### Spotify via Web API (spotipy)

`sources/spotify.py` risolve track, album, playlist e top track di un artista con la Web API ufficiale (spotipy) e delega l'audio al provider indicato in `[options.spotify] audio_provider` (o al primo con ricerca in `[priority]`). Non scarica mai audio da Spotify.

1. Crea un'app su https://developer.spotify.com/dashboard e copia Client ID e Client secret in `.env` (vedi `.env.example`).
2. `auth = "client_credentials"` basta per il catalogo pubblico. Per playlist private e brani salvati usa `auth = "pkce"` (scope `playlist-read-private playlist-read-collaborative user-library-read`, token in `token_cache_path`): al primo uso si apre il browser.
3. **L'account proprietario dell'app deve avere Spotify Premium.** Senza, la Web API risponde 403 "Active premium subscription required for the owner of the app" su ogni endpoint (track, album, artist, playlist); dopo l'attivazione possono servire alcune ore. Il provider lo riconosce e lo dice nell'hint.
4. Un 403 diverso significa che l'app è in Development Mode: aggiungi il tuo account in Dashboard → app → Settings → User Management, oppure richiedi l'Extended Quota Mode (necessaria per le playlist).

Le tracce senza match affidabile (sotto `unmatched_threshold`) finiscono in `unmatched.log` (`timestamp  url  query  punteggio  provider`) e vengono marcate `skipped`. I metadati risolti restano in cache SQLite per `cache_ttl_days` giorni (chiave = URL canonico). Gli ISRC vengono richiesti solo con `fetch_isrc = true` (`GET /tracks?ids=` a lotti da 50).

```bash
python -m core --dry-run "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy"
```

Smoke test reale (richiede le credenziali in `.env`): risolve una playlist pubblica, registra le risposte in una cassetta vcrpy con i token oscurati e confronta campo per campo la forma delle risposte reali con le fixture dei test offline. Esce con 1 se un campo letto dal provider manca o ha un altro tipo.

```bash
python scripts/spotify_smoke.py "https://open.spotify.com/playlist/<id>"
```

Stato al 2026-11-10: lo smoke è stato eseguito con credenziali reali, ma l'app usata non ha un proprietario Premium e Spotify ha risposto 403 a tutte le richieste di dati. Risultato: l'autenticazione funziona, la risposta reale del 403 è registrata in `tests/cassettes/spotify_403_premium_required.yaml` (token oscurati) ed è usata come fixture; il confronto campo per campo su tracce e playlist **non è ancora stato fatto**, quindi le fixture di `tests/test_spotify.py` restano sintetiche. Va rilanciato lo stesso comando quando l'account ha Premium.

### SoundCloud e Bandcamp

| Provider | URL | Delega (ricerca) | Note |
|---|---|---|---|
| `soundcloud` | soundcloud.com, m.soundcloud.com, on.soundcloud.com (brani, set, profili) | sì: `scsearch10:` (estrazione piatta) + matcher | i 429 sono retryable: backoff e breaker del core |
| `bandcamp` | `<artista>.bandcamp.com/album/...` e `/track/...` | no | legge `data-tralbum`; brani scaricabili → file diretto, altrimenti yt-dlp |

Opzioni in `[options.soundcloud]` (`cookiefile`, `ffmpeg_location`, `match_threshold`, `search_results`) e `[options.bandcamp]` (`cookiefile`, `ffmpeg_location`).

Nota Bandcamp: alle richieste dirette il sito può rispondere con una pagina di verifica ("Client Challenge") senza `data-tralbum`. In quel caso il provider passa a yt-dlp per leggere album e brani (download sempre via yt-dlp, mai diretto). Se anche yt-dlp viene bloccato, l'errore suggerisce di riprovare più tardi o usare `cookiefile`.

### Qualità e formati

- `best` tiene il formato nativo dello stream (es. AAC su SoundCloud, Opus o AAC su YouTube) senza ricodificare.
- Un codec esplicito preferisce lo stream nativo in quel codec (`bestaudio[acodec^=mp3]`); se coincide, yt-dlp copia senza ricodificare, quindi `mp3_320` su una sorgente mp3-128 resta a 128: non si inventa qualità. Se il codec è diverso si ricodifica una volta sola (lossy su lossy: scelta esplicita dell'utente).
- Bandcamp: `trackinfo[].file` è lo stream mp3-128 del player, non l'originale. Il download diretto si usa solo per `best`/`mp3` (≤128); l'originale FLAC/WAV passa dal flusso `/download` con token e non è implementato.

### Errori, retry e set

- Gli errori di yt-dlp passano da una tabella di pattern (`sources/_ytdlp.py`, `ERROR_RULES`): lo status HTTP decide per primo (429 e 5xx retryable; 401/403/404 permanenti), poi i pattern testuali (auth, geo-block, rimosso, ffmpeg, DRM). Un errore non riconosciuto viene ritentato una sola volta e loggato con il messaggio grezzo, così la tabella cresce dai casi reali. Il circuit breaker conta solo gli errori transitori.
- Set SoundCloud: estrazione completa con contenimento per brano (i mancanti vengono elencati, il set fallisce solo se manca la maggioranza) e riuso dell'`info` al download (nessuna seconda estrazione). Sopra `max_full_tracks` gli stream non vengono conservati.
- Link `on.soundcloud.com`: si segue il redirect e si classifica l'URL finale.
- Il matcher ordina in modo deterministico: punteggio, uploader = artista, durata più vicina, ordine di arrivo. `slowed`, `sped up`, `nightcore`, `8d`, `bootleg` sono penalizzati anche a durata uguale.

### Hardening

- **Cookie**: `--cookies <file Netscape>` e `--cookies-from-browser <browser[+keyring][:profilo]>` (CLI) o gli stessi parametri di `resolve`/`download`/`TaskQueue` nella façade valgono per tutti i provider; per provider resta `cookiefile` / `cookiesfrombrowser` in `[options.<nome>]`. Un 403 con verifica anti-bot è terminale (`NeedsCookies`): nessun retry finché non fornisci i cookie.
- **/audio**: sessione a cookie. `POST /login` valida utente e password (da `GRABMEDIA_AUTH_USER`/`GRABMEDIA_AUTH_PASSWORD` oppure da `nginx/.htpasswd`, formato `{SHA}` o testo in chiaro) e rilascia un cookie `HttpOnly`, `SameSite=Strict`, `Secure` su https, valido 30 minuti (`GRABMEDIA_SESSION_TTL`). `POST /logout` lo revoca, `GET /session` dice se si è connessi. Un middleware risponde 401 a `/audio` e ai suoi file senza sessione valida; dopo 5 tentativi falliti in un minuto il login risponde 429. Le sessioni stanno in memoria: si azzerano al riavvio e non sono condivise tra più processi. Nella pagina il riquadro di accesso compare quando `/audio` risponde 401; le credenziali non vengono mai salvate nel browser. Path dei file risolto e confinato nella cartella di output (symlink compresi), `Range` con 206/416 tramite Starlette. I download classici (`/download/...`) restano protetti dalla Basic Auth di nginx.
- **Anti-SSRF**: la façade risolve l'host e rifiuta loopback, privati, link-local, riservati (anche IPv6 e IPv4-mapped); host non risolvibili passano al provider. Nessuna allowlist.
- **Pin**: `yt-dlp[default]==2026.10.26` e `starlette==1.10.0` solo in `requirements.txt` (runtime unico di app e CLI); `requirements-dev.txt` usa intervalli per provare versioni nuove; il Dockerfile non fa più `--upgrade yt-dlp`. L'extra `[default]` porta `yt-dlp-ejs`, senza cui la signature di YouTube non si risolve nemmeno con deno.
- **Runtime JS**: rilevato all'avvio (`YTDLP_JS_RUNTIME`, poi deno, poi node) e passato a yt-dlp. `YTDLP_JS_RUNTIME` verso un binario inesistente produce un warning e si passa al rilevamento automatico; la web app senza runtime parte comunque e disabilita il provider YouTube, con le istruzioni di installazione nel messaggio. L'immagine Docker non include ancora deno.
- **Cookie del browser**: risolti una volta per sessione in un file Netscape nella directory dedicata (`GRABMEDIA_COOKIE_DIR`), nome con il pid, scadenza a 6 ore, pulizia all'uscita e su SIGTERM/SIGINT, sweep degli orfani all'avvio. Rinnovo esplicito: `--renew-cookies`, `renew_browser_cookies()` nella façade, `POST /audio/cookies/renew`.

### CI: job di bump di yt-dlp

`.github/workflows/bump-ytdlp.yml` gira ogni lunedì e a richiesta. Prova l'ultima release di yt-dlp con suite completa, verifica del runtime JS e canary YouTube con rete reale; se è verde apre una PR con il solo bump, se è rosso apre o commenta una sola issue con label `canary` e non mergia niente. Procedura, gate del canary e rollback sono in [docs/upgrading-ytdlp.md](docs/upgrading-ytdlp.md).

```bash
gh workflow run bump-ytdlp.yml -f force_checks=true
```
