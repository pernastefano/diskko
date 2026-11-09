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
  -v "$(pwd)/data":/data \
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
    mem_limit: 1g          # raise it for a very large library
    init: true
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
      PORT: "8000"
      # container paths (they win over the dev values in .env)
      DATA_DIR: /data
      MUSIC_DIR: /music
      DOWNLOADS_DIR: /downloads
      BASE_URL: ${BASE_URL:-http://localhost:8000}
      SLSKD_URL: http://slskd:5030
    volumes:
      - ./data:/data
      - /path/to/music:/music
      - ./downloads:/downloads   # shared with slskd
    depends_on:
      - slskd

  slskd:
    image: slskd/slskd:latest
    container_name: slskd
    restart: unless-stopped
    ports:
      - "127.0.0.1:5030:5030"   # slskd's own web page, for setting it up (DISKKO uses the Compose network)
      - "50300:50300"           # Soulseek peers; forward it on your router for full connectivity
    environment:
      SLSKD_REMOTE_CONFIGURATION: "true"
      SLSKD_DOWNLOADS_DIR: /downloads
      SLSKD_SLSK_USERNAME: ${SLSKD_SLSK_USERNAME}   # your Soulseek account
      SLSKD_SLSK_PASSWORD: ${SLSKD_SLSK_PASSWORD}
      SLSKD_USERNAME: ${SLSKD_USERNAME:-admin}      # slskd web page (default slskd/slskd is public knowledge)
      SLSKD_PASSWORD: ${SLSKD_PASSWORD}
      SLSKD_API_KEY: ${SLSKD_API_KEY}               # 16 to 255 characters; the same value goes in DISKKO
    volumes:
      - ./slskd:/app
      - ./downloads:/downloads   # same host folder as diskko
```

This is the repository's [`docker-compose.yml`](../docker-compose.yml). Put the secrets in an
`.env` next to it (Compose reads it, and so does DISKKO through `env_file`) and keep it out of
version control. Copy [`.env.example`](../.env.example) to `.env` and edit it: set `SECRET_KEY`, the
Soulseek and SLSKD values below, and `MB_USER_AGENT`. The container listens on `PORT=8000` (the
Compose file sets it, so the `8080` of the example file only applies to development). Update with `docker compose pull && docker compose up -d`; the database is
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

**SLSKD.** DISKKO talks to slskd's API; slskd talks to the Soulseek network. Before the first
request:

1. **A Soulseek account.** slskd needs a username and password for the Soulseek network (a free
   account: choose any free name). In the Compose file above they are `SLSKD_SLSK_USERNAME` and
   `SLSKD_SLSK_PASSWORD`; in slskd's own `slskd.yml` they are `soulseek.username` and
   `soulseek.password`. Without them slskd is not connected and every search finds nothing
   (DISKKO's **Status** page says so).
2. **An API key** (16 to 255 characters). `SLSKD_API_KEY` defines slskd's main key, which has the
   `Administrator` role, so it can search and download. Put the same value in **Settings →
   Integrations → SLSKD** together with the address (`http://slskd:5030`). If you define other keys in
   `slskd.yml` (`web.authentication.api_keys`), do not give DISKKO a read-only one.
3. **Change slskd's web login.** Its default is `slskd` / `slskd`; set `SLSKD_USERNAME` and
   `SLSKD_PASSWORD`. The Compose file publishes the web page only on `127.0.0.1`; DISKKO does not
   need it, it reaches slskd as `slskd:5030` on the Compose network.
4. **Forward port 50300** (the Soulseek listening port) on your router, or other users cannot
   connect to you and downloads are slower or fail more often.

Soulseek downloads are saved under `/downloads/diskko/<request id>/` and moved into the library
when they pass the checks.

## HTTPS and a reverse proxy

Put DISKKO behind a reverse proxy that terminates TLS. Set `BASE_URL` to the public `https://`
address: session cookies then get the `Secure` flag (they are always `HttpOnly` and
`SameSite=Lax`), and the app insists on a real `SECRET_KEY`.

Behind a proxy you control, also set `TRUST_PROXY=true` so the sign-in limit counts the browser's
address and not the proxy's. DISKKO takes the address your proxy appended to `X-Forwarded-For`
(the last entry; if several proxies are chained set `TRUST_PROXY_HOPS` to how many, e.g. `2` for a
CDN plus your proxy), never the left-most one, which a browser can write
itself. Never set it when DISKKO is reachable directly, or anyone could pick the address they are
counted as.

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

## Limits of the services DISKKO asks

Every service gets its own limiter, shared by all jobs, and a circuit breaker that pauses it after
repeated failures (5 in a row: ten minutes). A "too many requests" answer holds the service back for
everybody, and `Retry-After` is obeyed.

| Service | What it publishes | What DISKKO does |
|---------|-------------------|------------------|
| MusicBrainz | About 1 request per second per IP, a User-Agent with a contact (`MB_USER_AGENT`), 503 above it | One request every 1.1 s, always |
| Cover Art Archive | "No rate limiting rules" for now, but a 503 is documented | A request every 0.5 s; a timeout or 503 is tried once more |
| iTunes Search | About 20 calls a minute | One every 3.2 s; a refusal (403/429) pauses it for a minute |
| Last.fm | No number; "several calls per second" can suspend a key | One a second |
| ListenBrainz | One call per second, `X-RateLimit-*` headers, 429 | One every 1.1 s, and waits when the headers say none is left |
| Wikipedia and Wikidata | 200 a minute with a proper User-Agent (10 without), 429 with `Retry-After` | 120 a minute together; at least 5 s after a 429 |
| Deezer | No limit readable in the public pages (50 requests in 5 s is reported) | Four a second at most; the quota error is handled |

Covers that cannot be looked up because a service is down stay *unknown*; the nightly pass (04:50)
and opening the artist look them up again. A cover job that waits for a paused service does not use
its attempts.

## Health check

`GET /health` answers `200 {"status": "ok", "version": "..."}` when the app is up and its database
can be read, and `503` otherwise. The image has a `HEALTHCHECK` on it (`docker ps` shows
`healthy`), so an orchestrator can restart a container whose database is unreachable.

## Logs

The image logs one JSON object per line (`LOG_FORMAT=json`); set `LOG_FORMAT=text` for readable
lines. `LOG_LEVEL` defaults to `INFO`. Plex tokens, API keys, the secret key and `token=`/`api_key=`
pairs are scrubbed from every line, including tracebacks. Job logs, with their progress, are in
the **Jobs** page (icon in the top bar) and are kept for 1 to 60 days depending on the job (configurable).

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

DISKKO backs up its database every night at 03:15 in your `TZ` (`BACKUP_CRON`, empty to turn it off) into
`/data/backups/diskko-YYYYmmdd-HHMMSS.db` and keeps the newest 7 (`BACKUP_KEEP`). It uses SQLite's
online backup, so the copy is consistent while the app runs, and it is integrity-checked before it
appears. **Settings → Backups** lists the copies, makes one now and downloads any of them.

A backup holds everything in the database (users, requests, settings, the saved tokens and the
library index) and none of your music files. Copy it somewhere else regularly: a backup on the
same disk does not survive the disk.

To restore (this is for the bind-mounted `./data` of the Compose file; a downloaded backup must be
renamed to `app.db`):

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

Database migrations run at start-up and are not reversible. Back up first (**Settings → Backups →
Back up now**) before a major upgrade, and pin a version tag (`image: ghcr.io/pernastefano/diskko:v0.1.2`)
instead of `latest` so you can go back: stop DISKKO, restore the backup as above, set the previous
tag and start it again.

## Configuration reference

Environment variables are the defaults and the start-up values. Once an administrator saves a field
under **Settings**, the saved value wins and the environment value for that field is ignored (also
if you change it later in `.env`); change it in Settings. Some variables are not in Settings at
all and are read only from the environment: `ENV`, `SECRET_KEY`, `BASE_URL`, `PORT`, `DATA_DIR`,
`TZ`, `JOBS_ENABLED`, `SESSION_TTL_HOURS`, `MIN_PASSWORD_LENGTH`, `STATUS_CHECK_MINUTES`,
`LIBRARY_MAX_REMOVAL_PCT`, `SLSKD_STALL_MINUTES`, `SLSKD_QUEUE_MINUTES`, `SLSKD_MAX_HOURS`,
`SLSKD_FILE_RETRIES`, and the logging, sign-in and backup variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENV` | `development` | `production` refuses to start with the default `SECRET_KEY`. |
| `SECRET_KEY` | `change-me` | Signs session cookies. |
| `BASE_URL` | `http://localhost:8080` | Public address; `https://` makes cookies `Secure`. |
| `PUID`, `PGID` | `1000` | Container user and group. |
| `PORT` | `8000` in the image | Port inside the container (the Compose file sets it; `.env.example` says 8080 for development). |
| `TZ` | `UTC` | Time zone of the schedules (nightly scan, backup, cleanup). Backup file names are always UTC. |
| `JOBS_ENABLED` | `true` | `false` runs the web app without the job worker (development only). |
| `MIN_PASSWORD_LENGTH` | `8` | Shortest password accepted. |
| `DATA_DIR`, `MUSIC_DIR`, `DOWNLOADS_DIR` | | Container paths (see Volumes). |
| `QUARANTINE_DIR` | `<downloads>/diskko/quarantine` | Where downloads that cannot be imported wait. |
| `IMPORT_FOLDER_TEMPLATE`, `IMPORT_FILE_TEMPLATE` | `{artist}/{album} ({year})`, `{track:02d} - {title}` | Naming of imported albums. |
| `SESSION_TTL_HOURS` | `336` | Session length (sliding). |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_MAX_ATTEMPTS_IP`, `LOGIN_WINDOW_SECONDS` | `5`, `20`, `300` | Sign-in limit. |
| `TRUST_PROXY`, `TRUST_PROXY_HOPS` | `false`, `1` | Trust `X-Forwarded-For`, and how many proxies in front append to it. |
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
| `JOB_QUEUE_CONCURRENCY` | `default:2,musicbrainz:1,slskd:6,...,interactive:2` | Jobs running at once per queue (MusicBrainz is always 1). |
| `JOB_RETENTION_*_DAYS`, `AUDIT_RETENTION_DAYS` | `1`/`14`/`60`, `365` | How long finished jobs and the audit log are kept. |
| `STATUS_CHECK_MINUTES` | `5` | How often services are checked (0 turns it off). |

[`.env.example`](../.env.example) lists the ones you are most likely to set, with comments; the table above is the complete list of what you normally change (the rest, such as service URLs, has sensible defaults in `app/config.py`).

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `Permission denied` writing to `/music` or `/data` | `PUID`/`PGID` do not match the folder's owner. Fix them; `/data` is re-owned at start. |
| Container exits at start with a `SECRET_KEY` error | `BASE_URL` is https or `ENV=production` and `SECRET_KEY` is still the default. |
| Plex source finds no files | Plex's paths differ from yours: set `LIBRARY_PATH_MAP`. |
| A scan stops with *Is the music folder mounted?* | It would remove more than 50% of known tracks. Mount the folder, or raise `LIBRARY_MAX_REMOVAL_PCT` on purpose. |
| Downloads finish but are not imported | DISKKO and SLSKD do not share the downloads folder, or `DOWNLOADS_DIR` points elsewhere. |
| Everyone gets `429` at sign-in behind a proxy | Set `TRUST_PROXY=true`: otherwise all browsers share the proxy's address and its limit of 20 failed attempts. |
| The page is unstyled or buttons do nothing | The browser cannot reach the CDNs the interface loads Tailwind, HTMX and Alpine from (see *Good to know* in the README). |
| Albums listed twice after switching library source | The purge of the other source is skipped when the scanned source has fewer than half its tracks; fix the scan (mount, Plex path) and scan again. |
| Plex scans the whole library after an import | Plex refused a partial scan, did not show the album afterwards, or more than 20 folders were waiting; it falls back to scanning the music section once. |
| MusicBrainz lookups never start | `MB_USER_AGENT` is empty. |
| `docker ps` shows `unhealthy` | `docker logs diskko`: usually the database file is not writable. |
