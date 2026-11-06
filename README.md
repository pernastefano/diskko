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
