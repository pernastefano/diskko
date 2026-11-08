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
- **La sorgente scelta comanda**: dopo una scansione riuscita le righe lasciate da un'altra sorgente
  (per esempio la scansione da cartella dopo il passaggio a Plex) vengono tolte, così ogni album è
  elencato una volta. Restano gli artisti, i loro dati MusicBrainz e le decisioni manuali sugli
  abbinamenti; gli album nuovi di artisti già noti vengono abbinati subito. Se la sorgente
  scansionata ha meno della metà delle tracce dell'altra, nulla viene tolto e il log lo dice. Plex
  può elencare come due album una cartella con lo stesso titolo: si uniscono in uno.

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
  posseduti e per i tipi attivi (`COVERART_ENABLED=false` per disattivarle). Si scaricano quando
  apri la pagina di un artista e, per gli artisti che nessuno apre, con una passata notturna
  (alle 4:50, 200 artisti a notte) che riprova anche le copertine rimaste da scaricare, per esempio
  dopo un job annullato; quelle che l'archivio non ha non vengono richieste di nuovo.
- **Servizi di riserva**: per copertine, anteprime e artisti simili c'è un provider predefinito e
  una lista ordinata di fallback (copertine: Cover Art Archive poi Deezer; anteprime: iTunes poi
  Deezer; simili: Last.fm poi ListenBrainz), configurabile in Impostazioni → *Servizi* (o con `PROVIDERS_COVERS`, `PROVIDERS_PREVIEWS`, `PROVIDERS_SIMILAR`). Le
  copertine di Deezer si accettano solo se artista e titolo corrispondono quasi esattamente. Un
  servizio che fallisce 5 volte di fila (`PROVIDER_BREAKER_THRESHOLD`) viene messo in pausa per 10
  minuti (`PROVIDER_BREAKER_COOLDOWN_MINUTES`): nel frattempo si usano i fallback; se nessuno
  risponde il job torna in coda dopo la pausa **senza consumare un tentativo** e senza bloccare gli
  altri job.
- **Artisti e album che MusicBrainz non ha**: MusicBrainz resta sempre la fonte di riferimento, ma
  se non trova l'artista (nessun risultato) Diskko cerca lo stesso nome su Deezer e lo usa **solo se
  è certo**: nome identico e almeno un album della libreria nella sua discografia (con omonimi vince
  chi ne ha chiaramente di più, in caso di parità non si sceglie). Gli album arrivano con id
  `deezer:<id>`, hanno il tipo, l'anno e la copertina di Deezer, compaiono tra i mancanti e si
  possono richiedere come gli altri; non hanno la lista tracce (quella viene da MusicBrainz). Se
  l'artista c'è su MusicBrainz ma un album della libreria no, lo stesso controllo cerca quell'album
  su Deezer. L'artista passa in *Revisione artisti* come "Dati da Deezer, da verificare": si può
  **mantenere**, **cercare di nuovo su MusicBrainz**, indicare l'MBID giusto o segnarlo come non
  presente (i dati di Deezer vengono tolti). Alle sincronizzazioni periodiche si ricontrolla
  MusicBrainz e, se l'artista compare, i suoi dati sostituiscono quelli di Deezer. Si disattiva in
  Impostazioni → Servizi → *Dati di artisti e album* (`PROVIDERS_METADATA=musicbrainz`).

I dati di MusicBrainz sono di pubblico dominio (CC0); le copertine del Cover Art Archive hanno
licenze proprie e restano solo nella tua cache locale.

## Temi e icona

Il tema chiaro è **Sand** (neri caldi su crema, accento verde acqua) e quello scuro **Graphite**
(grafite con accento ambra), ispirati a TRAKKO; "Automatico" segue il sistema e la barra del browser
ne prende il colore. Il chiaro è la palette Tailwind impostata in `base.html`, lo scuro sono le regole
di `app/web/static/theme.css`. L'icona è un disco (un anello con un punto al centro) nello stile di
TRAKKO: `scripts/generate_icons.py` rigenera i PNG in `app/web/static/icons/` (solo libreria
standard), con `favicon.svg` e `manifest.webmanifest` per l'installazione come app.

## Libreria e pagina artista

`/library` (la pagina iniziale) mostra gli artisti a griglia o a elenco, con ricerca mentre scrivi,
barra A–Z, filtro "solo con album mancanti" e ordinamento per nome, mancanti o aggiunti di recente.
Le pagine da 48 artisti si caricano scorrendo, e funziona anche senza JavaScript con i link
Precedente/Successiva. `/artist/<id>` mostra l'immagine dell'artista, una **breve descrizione**
(al massimo due frasi, da Wikipedia nella lingua dell'interfaccia, altrimenti da Last.fm, sempre con
l'attribuzione), la **top 10 dei brani** con anteprima da 30 secondi (da Deezer; se non risponde,
lista di Last.fm con le anteprime che iTunes ha con certezza), poi gli album posseduti (titolo su una
riga, anno e tracce, etichetta di qualità) e quelli mancanti, raggruppati per tipo secondo le
preferenze dell'utente. Descrizione e top 10 si caricano in background alla prima visita e si
aggiornano (descrizione ogni 30 giorni, lista ogni 7; i link di anteprima di Deezer scadono dopo
pochi minuti e vengono rinnovati). Le sorgenti si scelgono in Impostazioni → Servizi. Quando si aggiornano i metadati di un artista (anche con la sincronizzazione
periodica), se non ha ancora un'immagine viene cercata su Deezer insieme alla descrizione. Il pulsante
"Richiedi download" è per ora solo un segnaposto: i download arrivano con le fasi successive.
Le copertine mancanti si scaricano in background quando si apre la pagina.

## Pagine

- **Dashboard** (`/dashboard`, la pagina iniziale): conteggi di artisti, album e tracce, album per
  qualità (ogni voce apre gli album con quella qualità) e ultima scansione. Gli amministratori
  hanno anche sorgente, cartella, frequenza e i pulsanti di scansione.
- **Artisti** (`/artists`): per ogni artista l'immagine (da Deezer, altrimenti una copertina o
  l'iniziale), il nome e "album posseduti / album totali" (totali = posseduti + ancora mancanti): **verde** se non manca nulla,
  **gialla** se ne hai alcuni e ne mancano altri, **rossa** se non ne hai nessuno.
- **Album** (`/albums`): tutti gli album posseduti, con ricerca e filtri per **qualità audio**,
  formato e tipo.
- **Gestione artisti** (`/admin/artists`, solo amministratori): abbinamenti con MusicBrainz e
  revisioni.
- **Pagina album** (`/album/<id>`): tabella delle tracce a colonne fisse (titolo, qualità, durata e
  lettore sempre a destra, senza andare a capo), con per ogni traccia **titolo – artista**
  (l'artista è un link alla sua pagina se lo conosciamo: serve alle compilation con più artisti;
  per gli album non posseduti gli artisti vengono da MusicBrainz). Si vede il profilo di qualità,
  non il formato del file. Gli amministratori hanno **Aggiorna metadati** (rilegge l'album su
  MusicBrainz e rinnova lista tracce, anteprime e copertina, cercandola se manca) e **Gestisci abbinamento** (apre la
  riga dell'album in Gestione artisti).
- Nella pagina di un album posseduto si può chiedere un **upgrade di qualità** (per esempio da MP3 a
  FLAC): parte una richiesta di download che cerca almeno quella qualità.

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
   sottocartella per disco); la richiesta diventa **Scaricato** e parte l'importazione (sotto).

Se non c'è nessun risultato la richiesta si può rilanciare con un altro testo di ricerca o
scegliendo a mano. `DOWNLOADS_DIR` è la cartella download come la vede Diskko: serve a verificare che i file
siano arrivati e a importarli (deve stare sullo stesso NAS, con `MUSIC_DIR` accessibile in scrittura). Soglie in `.env`: `SLSKD_STALL_MINUTES`,
`SLSKD_QUEUE_MINUTES`, `SLSKD_MAX_HOURS`, `SLSKD_FILE_RETRIES`.

## Più utenti: richieste, registro attività e notifiche

- **Richieste** (`/requests`): ogni utente vede le proprie, l'admin quelle di tutti (e può filtrare per
  utente). Filtri per stato (in corso, completate, fallite o annullate), 50 per pagina, e nella riga
  il pulsante Annulla (se è in corso) o Cerca di nuovo (se è fallita). L'admin può annullare e
  riprovare le richieste di chiunque; una richiesta riprovata resta dell'utente che l'aveva fatta.
  Le richieste finite (completate, fallite, senza risultati o annullate) si possono **eliminare** dalla
  lista, dal proprietario o da un admin; i file scaricati e la libreria non vengono toccati.
- **Qualità delle mie richieste**: in Preferenze ogni utente sceglie la qualità a cui puntano le sue
  richieste (o quella del sistema). I **tipi di album** "mancanti" restano invece uguali per tutti,
  come li imposta l'admin.
- **Registro attività** (`/admin/audit`, dal menu delle Impostazioni, solo admin): chi ha fatto cosa
  e quando: richieste (create, annullate, riprovate, scelte, upgrade), accessi riusciti e falliti
  (solo il nome usato), account (creati, ruolo, attivazione, password reimpostata, eliminati),
  impostazioni salvate (i nomi dei campi, mai i valori dei segreti), decisioni sugli artisti e sugli
  abbinamenti, aggiornamenti di album, scansioni manuali di libreria e di Plex. Filtri per utente,
  azione e data; non si registra l'indirizzo IP. Le voci si conservano 365 giorni
  (`AUDIT_RETENTION_DAYS`, in Impostazioni → Job) e la pulizia notturna le toglie.
- **Notifiche in app**: la campanella nell'intestazione conta quelle non lette e si aggiorna da sola.
  Il proprietario di una richiesta viene avvisato (una volta per evento) quando è completata, fallita,
  senza risultati o in attesa della sua scelta; `/notifications` le elenca, con "segna come letta"
  e "segna tutte". Le lette si cancellano dopo 30 giorni. Non ci sono quote per utente.

## Importare un download in libreria

Quando un download è completo parte il job `postprocess` (uno alla volta, nella coda `library`):

1. **Controllo dei file**: mutagen deve aprirli tutti con durata maggiore di zero; i file rovinati
   vengono elencati per nome. Si accettano solo file dentro la cartella di quella richiesta
   (`<downloads>/diskko/<id>`): link verso l'esterno e percorsi strani sono rifiutati.
2. **Qualità effettiva**, dai parametri reali dei file (codec, bit depth, frequenza, bitrate) e dai
   profili di qualità: conta il file peggiore. Sotto il minimo della richiesta si rifiuta; per un
   upgrade deve essere anche migliore di quella che hai. Non c'è analisi spettrale: un FLAC ottenuto
   da un MP3 non viene riconosciuto.
3. **Release MusicBrainz**: quella con tanti brani quanti sono i file e i titoli più simili (a parità,
   la più vecchia). Ogni file deve abbinarsi con certezza a un brano, altrimenti si rifiuta.
4. **Tagging** su una copia (gli originali restano intatti fino alla fine): titolo, artista (anche
   ospiti), artista dell'album, album, data, brano e disco con i totali, compilation, `releasetype` e
   gli MBID di release, release-group, artista e registrazione. I vecchi tag vengono tolti, tranne
   ReplayGain. Formati: FLAC, MP3 (ID3v2.4), M4A, OGG e Opus; gli altri (WAV, AIFF, APE...) si copiano
   senza modifiche. Senza release MusicBrainz (per esempio un album preso da Deezer) si scrive solo
   ciò che si sa dell'album.
5. **Copertina** incorporata in ogni file e salvata come `cover.jpg`: da Cover Art Archive (500 px),
   altrimenti un'immagine della cartella scaricata, altrimenti Deezer.
6. **Nomi** dai modelli in Impostazioni → Libreria → *Importazione dei download* (predefiniti
   `{artist}/{album} ({year})` e `{track:02d} - {title}`; con più dischi si aggiunge il disco davanti).
7. **Spostamento atomico**: la copia lavora in una cartella nascosta dentro la libreria (`.diskko-import`)
   e un solo `rename` la mette al suo posto. Poi si registrano album e tracce, si ricalcola
   l'abbinamento (l'album sparisce dai mancanti) e si cancella la cartella scaricata. Con una
   libreria Plex le righe le scrive la scansione, che viene accodata.

## Avvisare Plex

Dopo un import, se Plex è configurato (`PLEX_URL`, `PLEX_TOKEN`; vale anche con una libreria letta
da cartella), il job `plex_refresh` gli fa scansionare le cartelle nuove:

- **Debounce**: le cartelle importate si accumulano in un elenco e un solo job parte
  `PLEX_REFRESH_DEBOUNCE_SECONDS` (30) dopo la prima; gli import ravvicinati condividono la
  scansione, e se l'ultima è finita da poco si aspetta ancora.
- **Scansione parziale** della cartella dell'album, con il percorso come lo vede Plex
  (`LIBRARY_PATH_MAP` al contrario: `plex=locale`), poi attesa che Plex finisca (fino a
  `PLEX_REFRESH_TIMEOUT_MINUTES`, 15).
- **Verifica e ripiego**: Plex ignora in silenzio un percorso che non conosce, quindi si controlla
  che l'album sia comparso (cercandolo per titolo e confrontando la cartella delle tracce). Se Plex
  rifiuta la parziale, se l'album non compare o se le cartelle sono più di 20, si scansiona
  **l'intera sezione**, una sola volta; se ancora non compare c'è un avviso nel log.
- **Chiavi Plex** (`plex_rating_key`): con una libreria Plex il job accoda la scansione della
  libreria che le registra; con una libreria da cartella le scrive sull'album e sulle tracce
  importate, senza scansione completa. Con `PLEX_REFRESH_METADATA` chiede anche a Plex di aggiornare i
  metadati degli album.
- Un problema con Plex non fa mai fallire un import: il job si riprova da solo e le cartelle restano
  in elenco finché la scansione non è andata a buon fine.

Mai sovrascritto nulla: se la cartella di destinazione esiste, il download va in **quarantena**
(`<downloads>/diskko/quarantine/<id>/`, o la cartella scelta nelle impostazioni) con un file
`REASON.txt`; la richiesta risulta *fallita* con il motivo e il percorso. Lo stesso vale per file
rovinati, qualità insufficiente e brani che non si abbinano. Per un **upgrade** la vecchia copia non
viene cancellata: va in `<musica>/.diskko-replaced/<id>-<data>/` finché non la elimini tu.
Se l'import si interrompe a metà, rilanciando il job (Riprova) riprende senza copiare due volte.

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

## Job e log

Ogni job ha un'etichetta che dice su cosa lavora (artista, album...) e i log riportano i dettagli
(per esempio la copertina di quale album). Da **Job** si può cercare un testo in tutti i log; i job di
routine (controlli dei servizi, pulizia) sono nascosti finché non li si vuole vedere. Ogni notte
vengono cancellati i job conclusi più vecchi della conservazione, con i loro log: **1 giorno** per
i job di routine, **14** per gli altri riusciti, **60** per i falliti. I giorni (minimo 1) e un
valore per singolo tipo di job si cambiano in Impostazioni > Job.

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
- **Integrazioni**: Plex e SLSKD (libreria e download), MusicBrainz, Cover Art Archive e Last.fm
  (metadati e copertine), ciascuna con il proprio modulo; il test di connessione è nella pagina Stato.
- **Servizi**: provider predefinito e di riserva per metadati, copertine, anteprime e artisti simili,
  più la pausa dei servizi che falliscono. I token e le chiavi non vengono mai mostrati dopo il
  salvataggio: lasciando il campo vuoto si mantiene il valore attuale.
- **Job**: quanti job di ogni tipo girano insieme (MusicBrainz resta sempre a 1). Per quanto tempo si conservano i job finiti e i loro log: tre valori generali e, per ogni tipo di job, una casella numerica (vuota = valore generale), senza testo da scrivere.
- **Album**: i tipi di album considerati "mancanti" (uguali per tutti gli utenti).
- **Qualità**: i profili di qualità, completamente configurabili. Quelli standard sono *FLAC Master*
  (lossless, almeno 24 bit e 192 kHz), *FLAC Hi-Res* (almeno 24 bit e 96 kHz), *FLAC Standard*
  (almeno 16 bit e 44,1 kHz, il predefinito), *MP3 320 / V0* (almeno 320 kbps, V0 o lossless),
  *MP3 ≥ 256*, *MP3 ≥ 192* (o lossless) e *Qualsiasi*; si possono aggiungere, modificare (nome,
  requisiti, colore), riordinare, eliminare o ripristinare ai valori standard. Un file ottiene il
  miglior profilo che soddisfa e un album vale quanto la sua traccia peggiore. Ogni modifica avvia un
  job che ricalcola la libreria. Le installazioni esistenti passano ai valori standard con una
  migrazione, che lascia stare i profili modificati a mano.
- **Utenti**: creazione e reimpostazione della password in finestre modali, ruoli, attivazione ed
  eliminazione.

Le variabili d'ambiente (`.env`) restano i valori **predefiniti** e quelli di avvio (`SECRET_KEY`,
`BASE_URL`, `DATA_DIR`...); ciò che si salva dalle Impostazioni li sostituisce, e "Usa il
predefinito" li ripristina. I token salvati stanno nel database dell'app (nella cartella di
configurazione): trattala come riservata.

Ogni utente sceglie lingua e tema (Chiaro, Scuro o Automatico) dalle proprie **Preferenze**.
I livelli di qualità degli album già scansionati vengono ricalcolati automaticamente
all'aggiornamento.
