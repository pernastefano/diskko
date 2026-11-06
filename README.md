# DISKKO

Gestore di libreria musicale (Plex / filesystem) con album mancanti, download via SLSKD, tagging e rescan Plex.

## Sviluppo locale (senza Docker)

```bash
make install   # crea .venv e installa le dipendenze
make run       # http://localhost:8080  (/health per lo smoke test)
make test
make lint
```

## Deploy sul NAS

GitHub Actions builda l'immagine multi-arch e la pubblica su `ghcr.io/pernastefano/diskko`
(`latest` su main, `vX.Y.Z` sui tag, `dev` sugli altri branch). Sul NAS:
`docker compose pull && docker compose up -d` (app su porta 8000).

## Primo avvio

Al primo accesso l'app reindirizza a `/setup` per creare l'amministratore. Poi login su `/login`;
gli utenti si gestiscono da `/admin/users` (solo admin). In produzione imposta `SECRET_KEY`
con un valore casuale (obbligatorio se `BASE_URL` è https o `ENV=production`).
La lingua dell'interfaccia (English/Italiano) si cambia dal menu in alto; i cataloghi stanno in
`app/i18n/locales/`.

## Job e amministrazione

Tutto ciò che è lento o esterno gira come job asincrono (coda su SQLite, worker nello stesso
processo). Da `/admin/jobs` si vedono avanzamento e log in tempo reale e si possono annullare o
riprovare i job; `/admin/status` mostra le integrazioni (Plex, SLSKD, MusicBrainz) con il pulsante
"Test connessione" e lo stato delle code. I job interrotti da un riavvio riprendono da soli.
La concorrenza per coda si imposta con `JOB_QUEUE_CONCURRENCY`. Esegui **un solo processo** uvicorn
(niente `--workers N`), altrimenti lo scheduler partirebbe più volte.

## Libreria musicale

`/admin/library` mostra artisti, album e tracce trovati, la qualità degli album e l'ultima
scansione, e permette di avviarne una (o una riscansione completa). La scansione gira come job
(`scan_library`, coda `library`) ed è notturna di default (`LIBRARY_SCAN_CRON`).

- **Filesystem** (default): legge `MUSIC_DIR` con mutagen. Il rescan guarda solo dimensione e data
  dei file e rilegge i tag solo di quelli nuovi o modificati. Si ignorano cartelle nascoste e di
  sistema (`@eaDir`, `#recycle`, ...) e i link simbolici che escono dalla libreria.
- **Plex** (`LIBRARY_SOURCE=plex`): usa `PLEX_URL`, `PLEX_TOKEN` e `PLEX_MUSIC_SECTION`.
  Se Plex vede i file con percorsi diversi da quelli dell'app, imposta `LIBRARY_PATH_MAP`
  (es. `/data/music=/music`). Qualità e bit depth vengono dai metadati di Plex.
- **Protezione**: una scansione che toglierebbe più del 50% delle tracce note (`LIBRARY_MAX_REMOVAL_PCT`)
  si ferma senza modificare nulla, tipico caso del volume non montato.

## Album mancanti (MusicBrainz)

Con `MB_USER_AGENT` impostato (nome/versione e un contatto, richiesti da MusicBrainz) ogni
scansione accoda la sincronizzazione degli artisti nuovi, e una volta al giorno si aggiornano
quelli con metadati vecchi (`MB_REFRESH_DAYS`). Si rispetta il limite di **1 richiesta al
secondo** e le risposte restano in cache (`MB_CACHE_TTL_DAYS`).

- **Artisti**: l'MBID dei tag viene usato per primo; altrimenti si cerca per nome. Gli omonimi
  (ci sono cinque "Nirvana") si distinguono confrontando i tuoi album con le loro discografie.
  Quello che resta dubbio finisce nella **coda di revisione** (`/admin/artists/review`), dove si
  sceglie il candidato giusto, si incolla un MBID o si segna l'artista come "non su MusicBrainz".
- **Album**: ogni album posseduto è abbinato a un release-group (le edizioni deluxe e i remaster
  finiscono sullo stesso). Gli abbinamenti probabili contano come posseduti e si possono correggere
  a mano in `/admin/artists/<id>`; la scelta manuale non viene mai sovrascritta.
- **Mancanti**: i release-group dell'artista non posseduti, filtrati per tipo. Il default
  condiviso (album ed EP) si cambia in `/admin/library`; ogni utente può scegliere i propri tipi in
  `/account/preferences`.
- **Copertine**: scaricate dal Cover Art Archive in `CONFIG_DIR/covers` solo per gli album
  posseduti e per i tipi attivi (`COVERART_ENABLED=false` per disattivarle).

I dati di MusicBrainz sono di pubblico dominio (CC0); le copertine del Cover Art Archive hanno
licenze proprie e restano solo nella tua cache locale.

## Libreria e pagina artista

`/library` (la pagina iniziale) mostra gli artisti a griglia o a elenco, con ricerca mentre scrivi,
barra A–Z, filtro "solo con album mancanti" e ordinamento per nome, mancanti o aggiunti di recente.
Le pagine da 48 artisti si caricano scorrendo, e funziona anche senza JavaScript con i link
Precedente/Successiva. `/artist/<id>` mostra gli album posseduti (con badge di qualità, formato e
copertina) e quelli mancanti, raggruppati per tipo secondo le preferenze dell'utente. Il pulsante
"Richiedi download" è per ora solo un segnaposto: i download arrivano con le fasi successive.
Le copertine mancanti si scaricano in background quando si apre la pagina.
