# Development guide

## Requirements

- Python 3.12
- Nothing else. Docker is not needed to develop: the image is built by GitHub Actions (and the
  end-to-end stack runs there too, though you can run it locally if you have Docker).

## Setup

```bash
git clone https://github.com/pernastefano/diskko.git && cd diskko
make install                     # creates .venv and installs requirements-dev.txt
cp .env.example .env             # then edit: at least MB_USER_AGENT
source .venv/bin/activate
make run                         # http://localhost:8080
```

The first visit goes to `/setup` to create the administrator. Test data lives in `./data` and
`./music-test` (both git-ignored). Plex and SLSKD are reached over the network from your machine
(URLs in `.env`); the tests never call real services.

## Commands

| Command | What it does |
|---------|--------------|
| `make run` | Development server with reload on port 8080 (or `uvicorn app.main:app --reload --port 8080`). |
| `make test` | `pytest -q`. |
| `make lint` / `make format` | `ruff check .` / `ruff format .`. |
| `alembic revision --autogenerate -m "msg"` | New migration after a model change. |
| `alembic upgrade head` | Apply migrations (the app also does it at start-up). |

Dependencies are added with `pip` inside the venv and recorded in `requirements.txt` (runtime) or
`requirements-dev.txt` (tests, lint).

## Project layout

```
app/
  main.py, config.py, db.py, logging_config.py, models/, schemas/, quality.py
  auth/            sessions, Argon2, roles, CSRF, sign-in throttle
  jobs/            engine, worker, scheduler, handlers/ (one file per job type)
  integrations/    musicbrainz, coverart, lastfm, previews, plex, slskd (thin, mockable)
  services/        library scan, matching, missing albums, candidate selection,
                   importer/, backup, audit, notifications, ...
  web/             routers/ + templates/ + static/
  i18n/locales/    en.json, it.json (flat keys, same keys in both)
migrations/        Alembic versions
tests/             unit + integration; audio fixtures built on the fly; recorded HTTP replies
e2e/               docker-compose stack, mock Plex, mock SLSKD, the end-to-end driver
scripts/           icon generator, demo data, screenshots
docker/            entrypoint (PUID/PGID)
docs/              guides and assets
```

## Rules of the house

- **Everything slow or external is a job** (scan, MusicBrainz, SLSKD, tagging, Plex), never in an
  HTTP request. A job updates its progress and writes `job_logs`.
- **MusicBrainz: 1 request per second**, a configurable `User-Agent`, responses cached in `api_cache`.
- **Integrations sit behind thin interfaces** and are mocked in tests (`respx` for `httpx`,
  `responses` for `plexapi`). Read the official API documentation before implementing; do not guess
  payloads.
- **Never overwrite library files.** Validate every path (no traversal) and make imports atomic
  (copy to a temporary folder, then `rename`).
- **Secrets never reach logs or API responses.**
- **Paths come from configuration**, never hardcoded: the code runs the same locally and in the container.
- **Every schema change is an Alembic migration; every feature has tests.**
- **UI strings** go in both `app/i18n/locales/en.json` and `it.json` with the same flat keys; code and
  comments are in English.
- **Small commits**, one per logical step.

## Tests

```bash
pytest -q                          # everything
pytest tests/test_phase11.py -q    # one file
pytest -k backup -q                # by name
```

- Async tests run with `pytest-asyncio` (auto mode).
- Fixtures in `tests/conftest.py`: `app`, `client`, `admin_client`, `user_client`, `new_client`
  (extra browsers with their own cookies), `db`, `session_factory` (a migrated database without the
  web app), `settings`. `post(client, path, data, page=...)` sends a form with a valid CSRF token.
- Audio fixtures are generated on the fly (`tests/audio.py`); external replies are recorded or
  built in the test. No test calls a real service.
- Autouse fixtures reset the rate limiters, the circuit breakers and the sign-in throttle between tests.
- Run `ruff check . && ruff format --check .` before pushing; CI does.

### End-to-end tests

`e2e/` holds a Compose stack and a driver that run the product the way a person uses it, over HTTP:

- `e2e/docker-compose.yml`: DISKKO built from the repository (with `PUID=1234`), plus a **mock Plex**
  and a **mock SLSKD**, which are small FastAPI apps run with the same image
  (`e2e/mocks/mock_plex.py`, `e2e/mocks/mock_slskd.py`).
- `e2e/run_e2e.py`: first-run setup, sign-in, the sign-in limit, service status, a library scan
  through the mock Plex, a database backup and its download, and access control.

GitHub Actions runs it in the `e2e` job (`.github/workflows/docker.yml`) and also checks that the
container is healthy, runs as `1234:1234` and not root, and leaks no secret in its logs. To run it
yourself with Docker:

```bash
docker compose -f e2e/docker-compose.yml up -d --build --wait
pip install httpx && python e2e/run_e2e.py
docker compose -f e2e/docker-compose.yml down -v
```

Without Docker you can start the two mocks and the app by hand (`python e2e/mocks/mock_plex.py`,
`python e2e/mocks/mock_slskd.py`, then Uvicorn with the environment of the Compose file pointing at
`localhost`) and run the same driver.

## Container image and CI

`.github/workflows/docker.yml`:

1. **test**: `ruff check`, `ruff format --check`, `pytest`.
2. **e2e**: the stack above.
3. **build**: builds the image for the runner, starts it and checks `/health`, then (not on pull
   requests) pushes a multi-arch image (amd64 + arm64) to `ghcr.io/pernastefano/diskko` as `latest`
   on `main`, `vX.Y.Z` on tags and `dev` on other branches.

The `Dockerfile` is multi-stage (dependencies in a builder venv, a slim runtime). `docker/entrypoint.sh`
aligns `PUID`/`PGID`, owns `/data` and drops to that user with `gosu`; the image has a `HEALTHCHECK` on
`/health` and logs JSON.

## Releasing

```bash
git tag v0.2.0 && git push origin v0.2.0
```

The tag builds and publishes `ghcr.io/pernastefano/diskko:v0.2.0`. The version shown in the footer
and in `/health` is `app/__init__.py`'s `__version__`.

## Database

SQLite in WAL mode with foreign keys and a busy timeout (`app/db.py`). Migrations are in
`migrations/versions` and run at start-up.

### Writing migrations

```bash
alembic revision --autogenerate -m "describe the change"
```

Read the generated file: SQLite alters tables by recreating them, so data migrations and
constraint changes need care (the migration engine disables foreign keys while it runs, otherwise
dropping the old table would cascade). Add a test that upgrades from the previous revision when the
migration moves data.

## Jobs

A job type is one file in `app/jobs/handlers/` that registers with `@job_handler(name, queue=...)`
and is imported in `handlers/__init__.py`. Raise `RetryLater` to requeue without using an attempt
(a service paused by its circuit breaker), `PermanentJobError` to fail for good. Recurring jobs are
declared in `app/jobs/scheduler.py`. Add a label to `settings.jobtype.<name>` in both locale files
so it shows up in the job settings.

## Demo data and screenshots

```bash
export DATA_DIR=./demo-data
python scripts/seed_demo.py                       # fictional artists, covers drawn locally
JOBS_ENABLED=false uvicorn app.main:app --port 8100 &
pip install playwright                             # drives the Chrome you already have
python scripts/make_screenshots.py                 # writes docs/assets/screenshot-*.png
```

Sign in to the demo as `admin` or `marco` with `demo-password-1`. The banner and social preview in
`docs/assets` are plain SVG; the app icons come from `scripts/generate_icons.py`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| A job never starts | `JOBS_ENABLED=false` in your `.env`, or a second Uvicorn process owns the queue. |
| MusicBrainz sync does nothing | Set `MB_USER_AGENT` (name/version and a contact). |
| `database is locked` in a test | A test left a session open; use the `db` fixture or close it. |
| Port already in use | `make run PORT=8090`. |
| The UI looks unstyled offline | Tailwind, HTMX and Alpine come from CDNs in `base.html`; develop online. |
