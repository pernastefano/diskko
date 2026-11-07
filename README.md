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
- **Copertine**: scaricate dal Cover Art Archive in `DATA_DIR/covers` solo per gli album
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

## Richiedere un album (SLSKD)

Dalla pagina di un artista, "Richiedi download" su un album mancante avvia una richiesta (la
trovi in **Richieste**: ognuno vede le proprie, gli amministratori tutte):

1. **Ricerca** su Soulseek tramite SLSKD (URL e chiave API in Impostazioni > Integrazioni; la
   chiave deve avere il ruolo `ReadWrite` o `Administrator`).
2. **Selezione**: le cartelle trovate sono raggruppate per utente, si scartano quelle che non sono
   l'album (nome, numero di tracce) o che stanno sotto la qualità minima (i profili di qualità), e
   le altre ricevono un punteggio (nome, completezza, qualità, disponibilità dell'utente). I primi
   N candidati (default 5) sono salvati.
3. **Scelta**: in modalità automatica si scarica il migliore; in modalità manuale la richiesta si
   ferma su "Scegli" e si sceglie tra i candidati dalla pagina della richiesta.
4. **Download**: un job segue i trasferimenti. Se un utente è offline, un file continua a fallire,
   non arriva nulla per 10 minuti o si resta in coda remota per ore, passa al candidato successivo.
5. I file finiscono nella cartella download di SLSKD, in `diskko/<id richiesta>/` (una
   sottocartella per disco); la richiesta diventa **Scaricato**, in attesa dell'elaborazione.

Se non c'è nessun risultato la richiesta si può rilanciare con un altro testo di ricerca o
scegliendo a mano. `DOWNLOADS_DIR` è la cartella download come la vede Diskko (facoltativa: serve a
verificare che i file siano arrivati). Soglie in `.env`: `SLSKD_STALL_MINUTES`,
`SLSKD_QUEUE_MINUTES`, `SLSKD_MAX_HOURS`, `SLSKD_FILE_RETRIES`.

## Stato dei servizi

L'icona **Stato** nella barra in alto riassume la connessione ai servizi **configurati** (Plex,
SLSKD, MusicBrainz, Last.fm, ListenBrainz, iTunes e Deezer; quelli non configurati non contano):
**verde** se sono tutti raggiungibili, **gialla** se solo alcuni, **rossa** se nessuno, neutra finché
non c'è un risultato recente. Un job controlla i servizi all'avvio, ogni 5 minuti
(`STATUS_CHECK_MINUTES`, 0 per disattivare) e subito dopo ogni salvataggio delle Impostazioni; la
pagina Stato si aggiorna da sola e ha il pulsante "Controlla tutto ora".

L'icona **Job** è **gialla** se ci sono job in corso (in coda o in esecuzione) e **rossa** se ce ne
sono di falliti nelle ultime 24 ore, a meno che un job dello stesso tipo sia poi riuscito. Entrambe
le icone si aggiornano da sole ogni 15 secondi.

## Scoprire musica

- **Cerca** (voce nel menu): cerca qualsiasi artista su MusicBrainz, anche se non è nella libreria.
  "Apri" crea una pagina artista con la discografia da scaricare (secondo i tipi di album scelti
  nelle Impostazioni); gli artisti aperti ma mai aggiunti vengono dimenticati dopo 90 giorni.
- **Artisti simili**: da Last.fm (serve una chiave API gratuita, in Impostazioni > Integrazioni) e,
  se manca o non risponde, da ListenBrainz (serve il contatto in `MB_USER_AGENT`; usa un endpoint
  "labs" non documentato ufficialmente). Chi è già in libreria ha il badge "In libreria".
- **Pagina Album** (clic sulla copertina o sul titolo di un album): copertina, dettagli e tutte le
  tracce. Se l'album è **posseduto** ogni traccia si ascolta leggendo il file dal disco (FLAC, MP3,
  AAC/M4A, OGG/Opus, WAV, AIFF; per gli altri formati, come APE o WMA, la riga lo segnala: non c'è
  conversione). Se **non è posseduto** le tracce vengono da MusicBrainz e ognuna ha, dove esiste,
  l'**anteprima da 30 secondi** di iTunes o, in mancanza, di Deezer; c'è anche il pulsante
  "Richiedi download". L'audio delle anteprime viene sempre riprodotto direttamente dal provider e
  mai scaricato né salvato (gli indirizzi di Deezer scadono dopo circa 15 minuti e vengono richiesti
  di nuovo). I file locali sono serviti solo se stanno dentro la cartella della libreria.
- Tutte le chiamate esterne sono job (coda *Scoperta*, visibili in Job) con cache nel database.
  Last.fm, ListenBrainz, iTunes e Deezer hanno il test di connessione in Stato.
- Termini d'uso: Last.fm e Deezer ammettono solo uso non commerciale; iTunes richiede che le
  anteprime siano solo in streaming con attribuzione (mostrata sotto il player).

## Impostazioni

Gli amministratori configurano l'app da **Impostazioni** (l'ingranaggio in alto a destra), senza
riavviare:

- **Libreria**: cartella della musica (con "Verifica cartella"), sorgente predefinita (Filesystem o
  Plex) e frequenza della scansione (mai, ogni ora, ogni giorno, settimana, mese...).
- **Integrazioni**: Plex, SLSKD, MusicBrainz, Cover Art Archive e Last.fm, ciascuna con il proprio
  modulo e, dove esiste, il "Test connessione". I token e le chiavi non vengono mai mostrati dopo il
  salvataggio: lasciando il campo vuoto si mantiene il valore attuale.
- **Job**: quanti job di ogni tipo girano insieme (MusicBrainz resta sempre a 1).
- **Album**: i tipi di album considerati "mancanti" (uguali per tutti gli utenti).
- **Qualità**: i profili di qualità, completamente configurabili. Si parte da *FLAC Master* (24/192),
  *FLAC Hi-Res* (24/96), *FLAC Standard* (16/44,1, il predefinito), *MP3 320 / V0*, *MP3 ≥ 256*,
  *MP3 ≥ 192* e *Qualsiasi / bassa*; si possono aggiungere, modificare (nome, requisiti, colore),
  riordinare, eliminare o ripristinare. Un file ottiene il miglior profilo che soddisfa e un album
  vale quanto la sua traccia peggiore. Ogni modifica avvia un job che ricalcola la libreria. Le soglie
  di frequenza dei profili FLAC iniziali sono 88,2 e 176,4 kHz, così contano anche i multipli di 44,1.
- **Utenti**: creazione e reimpostazione della password in finestre modali, ruoli, attivazione ed
  eliminazione.

Le variabili d'ambiente (`.env`) restano i valori **predefiniti** e quelli di avvio (`SECRET_KEY`,
`BASE_URL`, `DATA_DIR`...); ciò che si salva dalle Impostazioni li sostituisce, e "Usa il
predefinito" li ripristina. I token salvati stanno nel database dell'app (nella cartella di
configurazione): trattala come riservata.

Ogni utente sceglie lingua e tema (Chiaro, Scuro o Automatico) dalle proprie **Preferenze**.
I livelli di qualità degli album già scansionati vengono ricalcolati automaticamente
all'aggiornamento.
