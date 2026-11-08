# Deployment

DISKKO runs as one container (or one Uvicorn process) with one SQLite file. The image is built and
published to the GitHub Container Registry (GHCR) by GitHub Actions; you never need Docker to
develop it.

## Run the container

```bash
docker run -d --name diskko \
  -p 8000:8000 \
  -e PUID=1000 -e PGID=1000 \
  -e SECRET_KEY='<a long random value>' \
  -e BASE_URL=http://localhost:8000 \
  -v ./data:/data \
  -v /path/to/music:/music \
  -v /path/to/slskd-downloads:/downloads \
  --restart unless-stopped \
  ghcr.io/pernastefano/diskko:latest
```

Open `http://localhost:8000`: the first visit asks you to create the administrator. Generate a
secret key with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Image tags

| Tag | Meaning |
|-----|---------|
| `latest` | The newest build of the `main` branch. |
| `vX.Y.Z` | A release (git tag `vX.Y.Z`). Prefer a version tag in production. |
| `dev` | The newest build of any other branch. |

The image is built for `linux/amd64` and `linux/arm64` (a Raspberry Pi or an ARM NAS works too).
Before publishing, the workflow runs the tests, then the end-to-end stack (DISKKO next to a mock
Plex and a mock SLSKD, see the [development guide](DEVELOPMENT.md#end-to-end-tests)), then starts
the image once and checks `/health`. If you fork the repository, the first published package is
private: change its visibility in the package settings on GitHub to pull it without logging in.

### Docker Compose with SLSKD

```yaml
services:
  diskko:
    image: ghcr.io/pernastefano/diskko:latest
    container_name: diskko
    restart: unless-stopped
    init: true
    mem_limit: 1g                  # raise it for a very large library
    security_opt: ["no-new-privileges:true"]
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}
    ports:
      - "8000:8000"
    env_file: .env
    environment:
      PUID: ${PUID:-1000}
      PGID: ${PGID:-1000}
      TZ: ${TZ:-Europe/Rome}
      BASE_URL: ${BASE_URL:-http://localhost:8000}
      SLSKD_URL: http://slskd:5030
      DATA_DIR: /data
      MUSIC_DIR: /music
      DOWNLOADS_DIR: /downloads
    volumes:
      - ./data:/data
      - /path/to/music:/music
      - ./downloads:/downloads     # shared with slskd
    depends_on: [slskd]

  slskd:
    image: slskd/slskd:latest
    container_name: slskd
    restart: unless-stopped
    ports: ["5030:5030", "5031:5031", "50300:50300"]
    environment:
      SLSKD_REMOTE_CONFIGURATION: "true"
      SLSKD_DOWNLOADS_DIR: /downloads
      SLSKD_API_KEY: ${SLSKD_API_KEY:-}
    volumes:
      - ./slskd:/app
      - ./downloads:/downloads     # the same host folder as diskko
```

The repository's [`docker-compose.yml`](../docker-compose.yml) is this file. Put the secrets in an
`.env` next to it (Compose reads it, and so does DISKKO through `env_file`) and keep it out of
version control. Update with `docker compose pull && docker compose up -d`; the database is
migrated at start-up.

## PUID and PGID

The container starts as root only to align with your files, then drops privileges before the app
runs (it never serves requests as root).

- Set `PUID` / `PGID` to the owner of your **music** folder and of the **downloads** folder
  (`id -u` and `id -g` on the host; on many NAS systems the user you share the folders with).
  The app then writes imported albums with that owner.
- The entrypoint creates the user and group if they do not exist, and changes the ownership of
  `/data` only (and only when something there belongs to someone else). `/music` and `/downloads`
  are your data and are never touched.
- `PUID=0` or `PGID=0` runs as root with a warning. Non-numeric values stop the container.

Check with `docker top diskko -o user,args`: the Uvicorn process must not be `root`.

## Volumes and paths

| Mount | Purpose | Needs |
|-------|---------|-------|
| `/data` | SQLite database (`app.db`), covers, `backups/` | Read/write. Keep it on a volume or a host folder. |
| `/music` | Your library | Read for scanning, **write** for imports. |
| `/downloads` | Where SLSKD saves | Must be the **same folder** SLSKD downloads into. |

All paths come from configuration (`DATA_DIR`, `MUSIC_DIR`, `DOWNLOADS_DIR`); nothing is hardcoded.

**Plex.** If Plex sees your music at another path than DISKKO does (it often does: `/data/music`
in Plex, `/music` here), set `LIBRARY_PATH_MAP` (`plex_prefix=local_prefix;...`, for example
`/data/music=/music`). The same map, reversed, is used to tell Plex which folder to scan after an
import. Plex does not need to run on the same machine, only to reach the same files.

**SLSKD.** Create an API key with the `ReadWrite` (or `Administrator`) role in its configuration and
paste it in **Settings → Integrations**. Soulseek downloads are saved under
`/downloads/diskko/<request id>/` and moved into the library when they pass the checks.

## HTTPS and a reverse proxy

Put DISKKO behind a reverse proxy that terminates TLS. Set `BASE_URL` to the public `https://`
address: session cookies then get the `Secure` flag (they are always `HttpOnly` and
`SameSite=Lax`), and the app insists on a real `SECRET_KEY`.

Behind a proxy you control, also set `TRUST_PROXY=true` so the sign-in limit counts the browser's
address and not the proxy's. Never set it when DISKKO is reachable directly, or anyone could pick
the address they are counted as.

Caddy:

```
diskko.example.com {
    reverse_proxy localhost:8000
}
```

nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name diskko.example.com;
    # ssl_certificate ...;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Run **one** Uvicorn process (the image does). The job worker and the scheduler live inside it, and
extra workers would run every scheduled job several times.

## Health check

`GET /health` answers `200 {"status": "ok", "version": "..."}` when the app is up and its database
can be read, and `503` otherwise. The image has a `HEALTHCHECK` on it (`docker ps` shows
`healthy`), so an orchestrator can restart a container whose database is unreachable.

## Logs

The image logs one JSON object per line (`LOG_FORMAT=json`); set `LOG_FORMAT=text` for readable
lines. `LOG_LEVEL` defaults to `INFO`. Plex tokens, API keys, the secret key and `token=`/`api_key=`
pairs are scrubbed from every line, including tracebacks. Job logs, with their progress, are in
**Admin → Jobs** and are kept for 1 to 60 days depending on the job (configurable).

## Security notes

- **Sign-in is rate-limited.** After `LOGIN_MAX_ATTEMPTS` (5) failed attempts for an account within
  `LOGIN_WINDOW_SECONDS` (300), or `LOGIN_MAX_ATTEMPTS_IP` (20) from one address, further attempts
  get `429 Too Many Requests` with a `Retry-After` header, even with the right password, until the
  window passes. Attempts are in the audit log. Set `LOGIN_MAX_ATTEMPTS=0` to turn the per-account
  limit off. The counters live in memory, so a restart clears them.
- **Passwords** are hashed with Argon2. There is no email, so an administrator resets forgotten
  passwords (**Settings → Users**).
- **Paths are validated.** Files are only read, served or imported when they resolve inside the
  library, the downloads area or the covers folder; `..`, absolute paths and symbolic links that
  point outside are refused, and a Soulseek peer cannot choose where a file is written.
- **Nothing in the library is overwritten.** Imports copy to a hidden staging folder and `rename`
  into place; an existing destination sends the download to quarantine instead.
- **Secrets** (Plex token, API keys) are never in logs or API responses, and never shown again after
  saving. They are stored in the database, so protect `/data` and its backups.
- `no-new-privileges` and a memory limit in the Compose file above are cheap extra hardening. The
  entrypoint needs to be root briefly to set the user up, so do not run with `user:` or `read_only`
  unless you have set `PUID`/`PGID` ownership up yourself and tested it.

## Backups and restore

DISKKO backs up its database every night at 03:15 (`BACKUP_CRON`, empty to turn it off) into
`/data/backups/diskko-YYYYmmdd-HHMMSS.db` and keeps the newest 7 (`BACKUP_KEEP`). It uses SQLite's
online backup, so the copy is consistent while the app runs, and it is integrity-checked before it
appears. **Settings → Backups** lists the copies, makes one now and downloads any of them.

A backup holds everything in the database (users, requests, settings, the saved tokens and the
library index) and none of your music files. Copy it somewhere else regularly: a backup on the
same disk does not survive the disk.

To restore:

```bash
docker compose stop diskko
cp data/backups/diskko-20260101-031500.db data/app.db
rm -f data/app.db-wal data/app.db-shm
docker compose start diskko
```

Newer versions migrate an older database at start-up. Run a library scan afterwards if the music
folder changed since the backup.

## Upgrading

```bash
docker compose pull && docker compose up -d
```

Database migrations run at start-up. Back up first (**Backups → Back up now**) before a major
upgrade. To go back, restore the backup and run the previous image tag.

## Configuration reference

Environment variables are the defaults and the start-up values; what an administrator saves under
**Settings** replaces them (and *Use default* brings them back). `SECRET_KEY`, `BASE_URL`,
`DATA_DIR`, `TZ` and the logging, sign-in and backup variables are read only from the environment.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENV` | `development` | `production` refuses to start with the default `SECRET_KEY`. |
| `SECRET_KEY` | `change-me` | Signs session cookies. |
| `BASE_URL` | `http://localhost:8080` | Public address; `https://` makes cookies `Secure`. |
| `PUID`, `PGID` | `1000` | Container user and group. |
| `TZ` | `UTC` | Time zone for schedules. |
| `DATA_DIR`, `MUSIC_DIR`, `DOWNLOADS_DIR` | | Container paths (see Volumes). |
| `QUARANTINE_DIR` | `<downloads>/diskko/quarantine` | Where downloads that cannot be imported wait. |
| `IMPORT_FOLDER_TEMPLATE`, `IMPORT_FILE_TEMPLATE` | `{artist}/{album} ({year})`, `{track:02d} - {title}` | Naming of imported albums. |
| `SESSION_TTL_HOURS` | `336` | Session length (sliding). |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_MAX_ATTEMPTS_IP`, `LOGIN_WINDOW_SECONDS` | `5`, `20`, `300` | Sign-in limit. |
| `TRUST_PROXY` | `false` | Trust `X-Forwarded-For`. |
| `LOG_FORMAT`, `LOG_LEVEL` | `text` (`json` in the image), `INFO` | Logging. |
| `BACKUP_CRON`, `BACKUP_KEEP` | `15 3 * * *`, `7` | Nightly backup. |
| `LIBRARY_SOURCE` | `filesystem` | `filesystem` or `plex`. |
| `LIBRARY_SCAN_CRON` | `0 2 * * *` | Nightly scan; empty turns it off. |
| `LIBRARY_MAX_REMOVAL_PCT` | `50` | A scan that would drop more of the known tracks stops (an unmounted folder). |
| `LIBRARY_PATH_MAP` | | `plex_prefix=local_prefix;...` |
| `PLEX_URL`, `PLEX_TOKEN`, `PLEX_MUSIC_SECTION` | `Music` | Plex. |
| `PLEX_REFRESH_ENABLED`, `PLEX_REFRESH_DEBOUNCE_SECONDS`, `PLEX_REFRESH_METADATA`, `PLEX_REFRESH_TIMEOUT_MINUTES` | `true`, `30`, `false`, `15` | Scan in Plex after an import. |
| `SLSKD_URL`, `SLSKD_API_KEY` | | SLSKD. |
| `SLSKD_MODE` | `auto` | `auto` downloads the best candidate; `manual` lets the user pick. |
| `SLSKD_SEARCH_TIMEOUT`, `SLSKD_CANDIDATES`, `SLSKD_STALL_MINUTES`, `SLSKD_QUEUE_MINUTES`, `SLSKD_MAX_HOURS`, `SLSKD_FILE_RETRIES` | `30`, `5`, `10`, `120`, `12`, `2` | Search and download tuning. |
| `MB_USER_AGENT` | | Required by MusicBrainz (app, version, a contact). |
| `LASTFM_API_KEY` | | Optional. |
| `PROVIDERS_COVERS`, `PROVIDERS_PREVIEWS`, `PROVIDERS_SIMILAR`, `PROVIDERS_TOPTRACKS`, `PROVIDERS_BIO`, `PROVIDERS_METADATA` | see Settings → Services | First is the default, the rest are fallbacks. |
| `JOB_QUEUE_CONCURRENCY` | `default:2,musicbrainz:1,slskd:6,...` | Jobs running at once per queue (MusicBrainz is always 1). |
| `JOB_RETENTION_*_DAYS`, `AUDIT_RETENTION_DAYS` | `1`/`14`/`60`, `365` | How long finished jobs and the audit log are kept. |
| `STATUS_CHECK_MINUTES` | `5` | How often services are checked (0 turns it off). |

[`.env.example`](../.env.example) lists them all with comments.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `Permission denied` writing to `/music` or `/data` | `PUID`/`PGID` do not match the folder's owner. Fix them; `/data` is re-owned at start. |
| Container exits at start with a `SECRET_KEY` error | `BASE_URL` is https or `ENV=production` and `SECRET_KEY` is still the default. |
| Plex source finds no files | Plex's paths differ from yours: set `LIBRARY_PATH_MAP`. |
| A scan stops with *Is the music folder mounted?* | It would remove more than 50% of known tracks. Mount the folder, or raise `LIBRARY_MAX_REMOVAL_PCT` on purpose. |
| Downloads finish but are not imported | DISKKO and SLSKD do not share the downloads folder, or `DOWNLOADS_DIR` points elsewhere. |
| Everyone gets `429` at sign-in behind a proxy | Set `TRUST_PROXY=true` so each browser is counted by its own address. |
| MusicBrainz lookups never start | `MB_USER_AGENT` is empty. |
| `docker ps` shows `unhealthy` | `docker logs diskko`: usually the database file is not writable. |
