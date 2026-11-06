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
