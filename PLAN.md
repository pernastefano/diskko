# Piano di progetto: DISKKO (music library manager)

Web app Python in Docker che legge la libreria musicale (filesystem o Plex), mostra per ogni artista gli album posseduti e mancanti, permette di richiederne il download via SLSKD, li tagga, li copia in libreria e fa ripartire lo scan di Plex. Include ricerca di artisti non posseduti, anteprime audio, artisti simili, job asincroni monitorabili e multiutente.

---

## 1. Decisioni architetturali

| Area | Scelta | Motivo |
|---|---|---|
| Web framework | **FastAPI** + Uvicorn | Async nativo, OpenAPI gratis |
| UI | **Jinja2 + HTMX + Alpine.js + Tailwind (CDN o build una tantum)** | Nessuna toolchain Node, ottimo per Claude Code, aggiornamenti live (polling/SSE) semplici |
| DB | **SQLite** (WAL mode) + **SQLAlchemy 2.0** + **Alembic** | Richiesto; WAL per concorrenza lettura/scrittura |
| Job queue | **Coda custom su SQLite** + worker asyncio in-process (nessun Redis/Celery) | Un solo container, job persistenti, riavviabili, monitorabili |
| Scheduler | **APScheduler** | Scan/refresh periodici |
| Auth | Sessioni a cookie firmato, **argon2-cffi**, ruoli `admin` / `user` | Multiutente semplice |
| Tag audio | **mutagen** | FLAC/MP3/M4A/OGG |
| Matching fuzzy | **rapidfuzz** | Associare file/Plex a MusicBrainz |
| HTTP client | **httpx** (async) + rate limiter + cache su DB | MusicBrainz limita a 1 req/s |
| Plex | **python-plexapi** | Lettura libreria + trigger scan |
| Test | **pytest**, pytest-asyncio, respx (mock HTTP) | |
| Lint | **ruff** + **mypy** (opzionale) | |

### Fonti dati esterne
- **MusicBrainz**: fonte di verità per artisti, release-group (tipo primario Album/Single/EP/Broadcast/Other + tipi secondari Live/Compilation/Soundtrack/Remix…), tracklist, MBID. Richiede `User-Agent` valido e max 1 req/s.
- **Cover Art Archive**: copertine via MBID.
- **Artisti simili**: Last.fm `artist.getSimilar` (API key gratuita) come primario; ListenBrainz come alternativa/fallback. Risultati con MBID quando disponibili, cache 7-30 giorni.
- **Anteprime audio (30 s)**: **iTunes Search API** (`previewUrl`, senza chiave) con fallback **Deezer API** (campo `preview`, senza chiave). Matching per artista+album+titolo traccia.
- **Plex**: token + URL server; sezione musica selezionabile.
- **SLSKD**: REST API (`/api/v0`) con API key.

> **Nota importante per Claude Code**: gli endpoint e i payload di SLSKD, Plex, MusicBrainz, Last.fm, iTunes e Deezer vanno **verificati sulla documentazione ufficiale / swagger di SLSKD (`/swagger`) prima di implementare**, non assunti a memoria.

---

## 2. Architettura logica

```
┌──────────────────────────── Container Docker ────────────────────────────┐
│  FastAPI (UI HTMX + REST /api)                                           │
│    ├─ Auth/Users/Roles                                                   │
│    ├─ Routers: library, artists, search, requests, admin, settings       │
│  Job Engine (asyncio workers + tabella jobs + log + progress)            │
│    ├─ scan_library      (filesystem | plex)                              │
│    ├─ sync_artist_metadata (MusicBrainz discography)                     │
│    ├─ fetch_similar_artists / fetch_previews / fetch_cover               │
│    ├─ search_slskd → select_candidate → download_monitor                 │
│    ├─ postprocess (verify → tag → rename → move)                         │
│    └─ plex_refresh                                                       │
│  Integrazioni: musicbrainz, coverart, lastfm, itunes/deezer, plex, slskd │
│  SQLite (/config/app.db)                                                 │
└───────────────┬──────────────────────┬──────────────────────┬────────────┘
         /music (rw)           /downloads (ro/rw, condiviso     /config
                                 con slskd)
```

### Volumi / rete
- `/config`: DB, log, cache copertine.
- `/music`: libreria (stesso path che Plex vede, oppure con **path mapping** configurabile).
- `/downloads`: cartella download di SLSKD **montata anche nell'app** (path mapping se i percorsi differiscono).
- Variabili: `PUID`, `PGID`, `SECRET_KEY`, `TZ`, `BASE_URL`.

---

## 3. Modello dati (SQLite)

- `users` (id, username, password_hash, role, is_active, created_at)
- `settings` (key, value_json, scope: `global`|`user`, user_id nullable)
- `artists` (id, mbid, name, sort_name, country, disambiguation, in_library bool, metadata_synced_at)
- `release_groups` (id, mbid, artist_id, title, primary_type, secondary_types_json, first_release_date, track_count_hint, cover_url)
- `library_albums` (id, artist_id, release_group_id nullable, path, title, year, format, bitrate/quality, track_count, plex_rating_key nullable, matched_confidence)
- `library_tracks` (id, album_id, path, title, track_no, disc_no, codec, bitrate, bit_depth, sample_rate, duration, mbid_recording)
- `similar_artists` (artist_id, similar_name, similar_mbid, score, source, fetched_at)
- `previews` (release_group_id / track, provider, url, fetched_at)
- `download_requests` (id, user_id, release_group_id, status, min_quality_profile, selected_candidate_json, slskd_username, slskd_dir, progress, error, created_at, updated_at)
  - stati: `queued → searching → candidate_found → downloading → postprocessing → importing → completed | failed | cancelled | no_results`
- `jobs` (id, type, payload_json, status, progress_pct, step, created_by, created_at, started_at, finished_at, error, parent_job_id, dedupe_key)
- `job_logs` (job_id, ts, level, message)
- `api_cache` (key, response, fetched_at, ttl)

Principio: la **libreria è condivisa** (un solo Plex/filesystem), mentre **richieste, preferenze e (opzionale) wishlist sono per utente**. Gli album mancanti sono calcolati globalmente; ogni utente può richiedere un download e vedere lo stato delle richieste (gli admin vedono tutto).

---

## 4. Impostazioni

**Globali (admin)**
- Sorgente libreria: `filesystem` | `plex` (+ path mapping)
- Percorsi: libreria, downloads, template di naming (`{artist}/{album} ({year})/{disc}{track:02d} - {title}`)
- Plex: URL, token, sezione musica, “scan parziale per path” on/off
- SLSKD: URL, API key, timeout ricerca, limiti concorrenza
- Last.fm API key, User-Agent MusicBrainz (contatto)
- Qualità minima di default e ordine di preferenza dei formati
- Tipi di album da considerare “mancanti” (default): Album, EP, Single, Live, Compilation, Soundtrack, Remix, Demo…

**Per utente**
- Tipi di album da mostrare come mancanti (override)
- Qualità minima richiesta per i propri download (override, se l’admin lo consente)
- Lingua UI, tema

**Profili di qualità** (ordinati): `Qualsiasi` < `MP3 ≥192` < `MP3 ≥256` < `MP3 320/V0` < `Lossless (FLAC)` < `Lossless 24-bit`.

---

## 5. Fasi di sviluppo

Ogni fase = una sessione/branch Claude Code, con test e commit. Non passare alla fase successiva senza criteri di accettazione soddisfatti.

### Fase 0: Setup repository e CLAUDE.md
- Struttura progetto, `pyproject.toml` (pip + venv, dipendenze in `requirements.txt`/`requirements-dev.txt` o extras), ruff, pytest, pre-commit, Makefile, `Dockerfile` e `docker-compose.yml` (app + slskd, **per il NAS**) e workflow GitHub Actions `.github/workflows/docker.yml` che builda e pubblica l'immagine multi-arch (amd64+arm64) su GHCR. **Lo sviluppo è locale, senza Docker sul Mac**.
- Creare `CLAUDE.md` (vedi sezione 7).
- **Accettazione**: `uvicorn app.main:app` (dentro il venv) avvia un’app “hello” su porta 8080; `pytest` e `ruff` passano; la GitHub Action builda l’immagine e fa uno smoke test (`/health`).

### Fase 1: Skeleton, DB, autenticazione multiutente
- Config via env + tabella `settings`; migrazioni Alembic; layout base Jinja/HTMX.
- Primo avvio: creazione admin (setup wizard). Login/logout, gestione utenti (CRUD solo admin), cambio password, CSRF.
- Dependency `require_user` / `require_admin`.
- **Accettazione**: due utenti con ruoli diversi; le pagine admin sono negate agli utenti; test auth.

### Fase 2: Job engine e pagina Amministrazione (prima di tutto il resto)
- Tabelle `jobs`/`job_logs`; worker asyncio con concorrenza configurabile e **code per tipo** (es. MusicBrainz = 1 worker per rispettare il rate limit; SLSKD separato).
- API `enqueue(type, payload, dedupe_key)`, `update_progress`, `log`, cancellazione cooperativa, retry con backoff, **ripresa dei job interrotti al riavvio**.
- Pagina **/admin/jobs**: lista live (HTMX polling o SSE), filtri per tipo/stato, progress bar, dettaglio con log in streaming, pulsanti annulla/riprova. Pagina **/admin/status**: stato integrazioni (Plex, SLSKD, MusicBrainz) con “test connessione”.
- Scheduler per job ricorrenti (scan notturno, refresh metadati).
- **Accettazione**: job dimostrativo di 30 s con progress e log visibili live; sopravvive al riavvio del container.

### Fase 3: Scansione libreria (filesystem + Plex)
- Interfaccia `LibraryProvider` con due implementazioni:
  - **Filesystem**: walk di `/music`, lettura tag con mutagen, raggruppamento per album (tag `albumartist`/`album`/MBID se presenti, fallback struttura cartelle), scan incrementale (mtime/size) e rilevazione file rimossi.
  - **Plex**: lettura artisti/album/tracce via plexapi (con GUID MusicBrainz se disponibili), path mapping.
- Salvataggio in `artists`, `library_albums`, `library_tracks`; calcolo qualità (codec/bitrate/bit-depth).
- Job `scan_library` con progress per artista, avviabile da admin e schedulabile.
- **Accettazione**: scan di una libreria di test (fixture di file tag-ati generati) con risultati corretti, rescan incrementale rapido.

### Fase 4: Metadati MusicBrainz e calcolo album mancanti
- Client MusicBrainz con rate limiter, retry, cache; risoluzione artista → MBID (tag MBID prima, poi ricerca con scoring fuzzy e **casi ambigui in coda di revisione manuale** nell’UI admin).
- Job `sync_artist_metadata`: scarica i release-group dell’artista (paginazione) con tipi primari/secondari; salva copertine (Cover Art Archive, cache locale).
- **Matching** album posseduti ↔ release-group (titolo normalizzato, anno, n. tracce; gestione edizioni deluxe/remaster) con `matched_confidence` e override manuale.
- Calcolo “mancanti” = release-group dell’artista non matchati, filtrati per tipi scelti in configurazione (globale/utente).
- **Accettazione**: per artisti campione l’elenco mancanti è plausibile; cambiare i tipi in impostazioni cambia subito il risultato; test con risposte MusicBrainz registrate.

### Fase 5: UI libreria e pagina artista
- **/library**: griglia/lista artisti con ricerca, filtro, contatori (posseduti/mancanti).
- **/artist/{id}**: sezioni *Posseduti* (con badge qualità) e *Mancanti* (raggruppati per tipo), copertine, pulsante “Richiedi download” (solo UI, collegata alla fase 8), indicatore stato richiesta, pulsante “Aggiorna metadati”.
- Sezione **Artisti simili** (placeholder, completata in fase 6).
- **Accettazione**: navigazione fluida su libreria di 1000+ artisti (paginazione/lazy load).

### Fase 6: Ricerca globale, artisti simili, anteprime
- Ricerca artisti su MusicBrainz (anche non posseduti) → pagina artista “esterno” con **tutti** gli album come non posseduti e pulsante download.
- Artisti simili da Last.fm/ListenBrainz con link interno (posseduto) o esterno (non posseduto), badge “in libreria”.
- Anteprima 30 s: player HTML5 per album (prima traccia/traccia selezionabile) con fallback iTunes → Deezer; risoluzione lazy e cache; un solo audio alla volta.
- Tutte le chiamate via job/cache, con stato visibile in admin.
- **Accettazione**: cercando un artista non posseduto si vedono discografia, simili e si ascolta un’anteprima.

### Fase 7: Integrazione SLSKD
- Client SLSKD async: ricerca (`POST` search, polling dello stato, lettura risposte), enqueue download, monitoraggio transfer, cancellazione, health check.
- **Motore di selezione candidati** (modulo isolato e molto testato):
  1. query: `artista album` (+ varianti normalizzate, fallback senza punteggiatura/anno);
  2. raggruppare i file per `username + directory`;
  3. scartare cartelle non audio, con tracce insufficienti rispetto al `track_count` atteso, formati sotto la qualità minima;
  4. punteggio: corrispondenza nome cartella/titoli tracce, completezza (n. tracce), qualità (FLAC > MP3 320 > …), slot libero, coda, velocità upload, penalità per file `.m3u`, “various”, ecc.;
  5. se nessuno supera la soglia → stato `no_results` con possibilità di retry/ricerca manuale.
- Opzione “scelta manuale”: mostrare la top-N dei candidati all’utente prima del download.
- Job `download_monitor`: segue i transfer, gestisce stalli/timeout, tentativo con il secondo candidato in caso di fallimento.
- **Accettazione**: contro un SLSKD reale (o mock HTTP) una richiesta passa da `queued` a `downloading` e arriva alla cartella downloads con progress in admin.

### Fase 8: Post-processing, tagging e import
- Job `postprocess`: verifica integrità (mutagen apre tutti i file, durata > 0, n. tracce vs MusicBrainz), **rilevamento qualità effettiva**, rifiuto se sotto soglia.
- **Tagging** da release MusicBrainz scelta (artista, album artist, album, anno, tracce, disco, titolo, MBID release/recording/artist/release-group, `releasetype`), embedding cover (CAA, con fallback a cover trovata nella cartella), rimozione tag spuri, nomi file da template.
- Spostamento atomico in `/music/{Artista}/{Album (Anno)}/` (gestione conflitti/duplicati, mai sovrascrivere senza conferma), pulizia della cartella download, aggiornamento DB (`library_albums`), stato `completed`.
- Gestione errori con cartella `quarantine` e messaggio chiaro.
- **Accettazione**: un album scaricato risulta taggato correttamente (verifica con mutagen nei test), nel posto giusto e sparisce dai “mancanti”.

### Fase 9: Integrazione Plex (refresh)
- Dopo l’import: scan **parziale** della sola cartella dell’artista/album (endpoint refresh con `path`) con fallback a scan sezione completa; debounce per import ravvicinati (un solo scan ogni N secondi).
- Job `plex_refresh` visibile in admin; opzione per aggiornare i metadati di Plex dopo lo scan e riconciliare `plex_rating_key`.
- **Accettazione**: dopo un import l’album compare in Plex (test manuale + test con mock).

### Fase 10: Multiutente, permessi e rifinitura
- Richieste per utente, pagina **/requests** (storico e stato; admin vede tutto, può annullare/riprovare), quote/limiti opzionali per utente, notifiche in-app al completamento (webhook/Discord opzionale).
- Preferenze per utente (tipi album, qualità), wishlist/“segui artista” opzionale con controllo periodico di nuove uscite.
- Audit log essenziale (chi ha richiesto cosa).
- **Accettazione**: due utenti con preferenze diverse vedono “mancanti” diversi e richiedono album indipendentemente.

### Fase 11: Hardening, packaging, documentazione
- Immagine Docker multi-stage (buildata da CI), utente non root con PUID/PGID, healthcheck, log JSON, limiti memoria, backup DB (job + download dal pannello admin).
- Sicurezza: segreti mai nei log, validazione path (no path traversal nei move/import), rate limit sul login, cookie `HttpOnly/SameSite`, cifratura a riposo opzionale dei token in `settings`.
- Test end-to-end **in CI (GitHub Actions)** con docker-compose di test (SLSKD mock + Plex mock), README con setup, esempi compose per NAS, troubleshooting.
- **Accettazione**: installazione da zero seguendo solo il README; suite di test verde in CI.

---

## 6. Ordine consigliato e milestone

| Milestone | Fasi | Risultato utilizzabile |
|---|---|---|
| M1 “Vedo la mia libreria” | 0–3 | Login, admin job, libreria scansionata |
| M2 “Vedo cosa manca” | 4–5 | Album mancanti per artista |
| M3 “Scopro musica” | 6 | Ricerca, simili, anteprime |
| M4 “Scarico davvero” | 7–9 | Download → tag → libreria → Plex |
| M5 “Pronto per tutti” | 10–11 | Multiutente, hardening, docs |

Suggerimento: dopo M2 usare davvero l’app sulla propria libreria per validare la qualità del matching, che è il punto più delicato del progetto.

---

## 7. File `CLAUDE.md` da mettere nella root del repo

```markdown
# DISKKO

Web app Python/FastAPI in Docker: legge libreria musicale (filesystem o Plex), mostra album
posseduti/mancanti per artista (MusicBrainz), richiede download via SLSKD, tagga, importa e
fa rescan di Plex. Multiutente. DB SQLite. Job asincroni monitorabili in /admin.

## Stack
Python 3.12 (venv + pip), FastAPI, SQLAlchemy 2 + Alembic, SQLite (WAL), Jinja2 + HTMX + Alpine + Tailwind,
httpx, mutagen, rapidfuzz, python-plexapi, APScheduler, pytest, ruff.

## Comandi
- Avvio dev: `uvicorn app.main:app --reload --port 8080`
- Test: `pytest -q`
- Lint/format: `ruff check . && ruff format .`
- Migrazioni: `alembic revision --autogenerate -m "msg"` / `alembic upgrade head`
- Docker: NON disponibile sul Mac di sviluppo. L'immagine è buildata da GitHub Actions (multi-arch) e pubblicata su GHCR; il deploy è sul NAS. Non proporre comandi `docker` da eseguire in locale: modifica Dockerfile/compose con cura e verifica via CI.

## Struttura
app/
  main.py, config.py, db.py, models/, schemas/
  auth/            # sessioni, argon2, ruoli
  jobs/            # engine, worker, scheduler, handlers/ (un file per tipo di job)
  integrations/    # musicbrainz.py, coverart.py, lastfm.py, previews.py, plex.py, slskd.py
  services/        # library_scan, matching, missing_albums, candidate_selection, tagging, importer
  web/             # routers + templates/ + static/
tests/             # unit + integration, fixture audio generate al volo, risposte HTTP registrate

## Sviluppo locale
- Python 3.12 di sistema con virtualenv in .venv (attivato con `source .venv/bin/activate`); aggiungi le dipendenze con pip e aggiorna i requirements. Dati di test in ./config e ./music-test (git-ignored).
- Plex/SLSKD si raggiungono via rete dal Mac (URL nel file .env); per i test usa mock.
- Il codice deve funzionare uguale in locale e nel container: tutti i percorsi vengono da configurazione, mai hardcoded.

## Regole
- Tutto ciò che è lento o esterno (scan, MusicBrainz, SLSKD, tagging, Plex) è un JOB: mai nelle
  request HTTP. Ogni job aggiorna progress e scrive job_logs.
- Rispettare rate limit MusicBrainz (1 req/s, User-Agent configurabile). Cache su `api_cache`.
- Le integrazioni stanno dietro interfacce sottili e sono mockabili (respx). Nessun test chiama
  servizi reali.
- Verifica API esterne (SLSKD swagger, Plex, MusicBrainz, Last.fm, iTunes, Deezer) sulla doc
  ufficiale prima di implementare; non indovinare payload.
- Mai sovrascrivere file in libreria senza controllo; validare sempre i path (no traversal);
  operazioni di import atomiche (copia su tmp + rename).
- Segreti (token Plex, API key) mai nei log né nelle risposte API.
- Libreria condivisa; richieste/preferenze per utente. Admin vede tutto.
- Ogni modifica allo schema = migrazione Alembic. Ogni feature = test.
- Italiano per l'interfaccia utente (con i18n predisposta), inglese per codice e commenti.
- Piccoli commit, uno per step logico. Lavora una fase alla volta seguendo PLAN.md.
```

---

## 8. Come lavorare con Claude Code (istruzioni operative)

1. **Prepara il repo**: crea la cartella, `git init`, copia questo documento come `PLAN.md` e la sezione 7 come `CLAUDE.md`.
2. **Una fase per volta, in Plan Mode**: avvia `claude`, premi Shift+Tab per entrare in plan mode, chiedi il piano della fase, rivedilo, poi approva l’esecuzione. Alla fine di ogni fase: `pytest`, lint, commit, e `/clear` (o nuova sessione) per tenere il contesto pulito.
3. **Affida le verifiche a subagent** dove utile (es. “leggi la documentazione SLSKD e riassumi gli endpoint di ricerca/download” prima della Fase 7).
4. **Test prima dei punti rischiosi**: motore di matching (Fase 4), selezione candidati (Fase 7) e tagging (Fase 8) vanno sviluppati in TDD con casi reali raccolti dalla tua libreria.
5. **Dati reali ma sicuri**: tieni una libreria di prova ridotta (20–50 album) per i test manuali; non dare mai a Claude Code i tuoi token Plex/SLSKD in chiaro nel repo: usa `.env` (in `.gitignore`).

### Prompt iniziali pronti all’uso

**Prompt 0 (bootstrap)**
> Leggi PLAN.md e CLAUDE.md. Esegui la Fase 0: crea la struttura del progetto, pyproject.toml, Dockerfile multi-stage, docker-compose.yml (app + slskd di esempio con volume downloads condiviso), ruff, pytest e una pagina “hello” FastAPI su :8080. Mostrami prima il piano, poi implementa. Alla fine fai girare test e lint e committa.

**Prompt 1 (auth)**
> Fase 1 di PLAN.md. Implementa DB SQLAlchemy+Alembic, setup wizard del primo admin, login con sessioni cookie e argon2, ruoli admin/user, CRUD utenti per admin, protezione CSRF e layout base Jinja+HTMX+Tailwind. Scrivi test per permessi e flussi di login.

**Prompt 2 (job engine)**
> Fase 2 di PLAN.md. Implementa il job engine su SQLite con worker asyncio, code per tipo, concorrenza configurabile, progress, log, cancellazione cooperativa, retry con backoff, dedupe_key e ripresa dopo riavvio. Crea /admin/jobs con aggiornamento live (HTMX polling o SSE), dettaglio con log, annulla/riprova, e /admin/status con test connessione. Aggiungi un job demo di 30 secondi e test sul recovery.

**Prompt 3 (scan)**
> Fase 3 di PLAN.md. Definisci l’interfaccia LibraryProvider e implementa FilesystemProvider (mutagen, scan incrementale) e PlexProvider (plexapi, path mapping). Crea il job scan_library con progress per artista e le tabelle necessarie con migrazione. Genera fixture audio nei test (file minimi taggati) e verifica raggruppamento, qualità e rescan incrementale.

**Prompt 4 (MusicBrainz e mancanti)**
> Fase 4 di PLAN.md. Prima consulta la doc ufficiale MusicBrainz (rate limit, release-group, tipi primari/secondari). Implementa client con rate limiter e cache, risoluzione artista→MBID con coda di revisione per i casi ambigui, job sync_artist_metadata, matching album posseduti↔release-group con confidence e override manuale, e il calcolo dei mancanti filtrato per tipi configurabili (globale e per utente). Test con risposte HTTP registrate.

**Prompt 5–6 (UI, ricerca, simili, anteprime)**
> Fasi 5 e 6 di PLAN.md. Costruisci /library e /artist/{id} (posseduti/mancanti per tipo, copertine, badge qualità, pulsante richiedi), ricerca globale artisti non posseduti, artisti simili (Last.fm con fallback ListenBrainz) con badge “in libreria”, e anteprime 30 s via iTunes Search API con fallback Deezer. Tutte le chiamate esterne come job con cache.

**Prompt 7 (SLSKD)**
> Fase 7 di PLAN.md. Leggi lo swagger/documentazione di SLSKD e riassumi gli endpoint prima di scrivere codice. Implementa il client, il motore di selezione candidati come modulo puro e testato (raggruppamento per utente+cartella, filtro per qualità minima e numero tracce, punteggio, fallback sul secondo candidato) e il job download_monitor con gestione di stalli e timeout. Includi la modalità “scegli manualmente tra i primi N candidati”.

**Prompt 8–9 (import e Plex)**
> Fasi 8 e 9 di PLAN.md. Implementa il job postprocess: verifica integrità e qualità effettiva, tagging da release MusicBrainz con cover embedded, rename da template, spostamento atomico in libreria con gestione conflitti e quarantena, aggiornamento DB. Poi plex_refresh con scan parziale per path, debounce e fallback a scan completo. Test sul tagging leggendo i tag finali con mutagen.

**Prompt 10–11 (multiutente e hardening)**
> Fasi 10 e 11 di PLAN.md. Completa richieste per utente, pagina /requests, preferenze per utente, audit log, quote opzionali; poi hardening (PUID/PGID, healthcheck, validazione path, rate limit login, backup DB), test end-to-end con docker-compose e README completo.

---

## 9. Rischi e punti di attenzione

- **Matching libreria ↔ MusicBrainz**: edizioni deluxe, remaster, “Various Artists”, nomi con featuring, alfabeti non latini. Mitigazione: MBID nei tag come prima scelta, confidence score, revisione manuale, override persistente.
- **Qualità dei risultati su Soulseek**: nomi cartella non standard, album incompleti, utenti lenti. Mitigazione: punteggio + fallback + scelta manuale.
- **Rate limit e cortesia verso API gratuite**: cache aggressiva, un solo worker MusicBrainz, backoff.
- **Anteprime non sempre disponibili** (specialmente per album di nicchia): UI deve degradare con grazia.
- **Path mapping** tra container app, SLSKD e Plex: è la causa più comune di errori; prevedere un “test percorsi” nella pagina admin.
- **SQLite e concorrenza**: WAL, transazioni brevi, un solo writer logico per le scritture massive (scan).
- **Aspetti legali**: il download tramite reti P2P può essere soggetto a restrizioni sul copyright nel tuo paese; l’uso è responsabilità dell’utente (valuta di dichiararlo nel README).

---

## 10. Build e deploy sul NAS (senza Docker sul Mac)

Flusso: `git push` su GitHub -> GitHub Actions builda l'immagine (amd64 + arm64) -> pubblica su `ghcr.io/TUO_UTENTE/diskko` -> sul NAS fai `docker compose pull && docker compose up -d`.

- Tag: `latest` sul branch main, `vX.Y.Z` sui tag git, `dev` sugli altri branch (per provare una versione sul NAS prima del merge).
- Il pacchetto GHCR è privato per default. Sul NAS: `docker login ghcr.io -u TUO_UTENTE` con un Personal Access Token che ha solo lo scope `read:packages`. In alternativa rendi pubblico il pacchetto (il codice resta privato).
- Aggiornamenti automatici opzionali sul NAS con Watchtower.
- Limite: i problemi tipici del container (permessi sui volumi, PUID/PGID, path mapping tra app, SLSKD e Plex) emergono solo sul NAS. Mitigazione: smoke test in CI, pagina admin "test percorsi", tag `dev` per prove.
