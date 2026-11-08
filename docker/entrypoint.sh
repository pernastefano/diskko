#!/bin/sh
# Starts as root only to align PUID/PGID with the host's owner of the volumes, then drops
# privileges (gosu) before the app runs. /music and /downloads are user data and never touched.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID$PGID" in
    ''|*[!0-9]*) echo "error: PUID and PGID must be numbers (got PUID=$PUID PGID=$PGID)" >&2; exit 1 ;;
esac

if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    echo "warning: running as root (PUID/PGID 0); set them to your host user's ids" >&2
    exec "$@"
fi

# Create (or reuse) a group/user with the requested ids.
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" diskko
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin diskko
fi

# Only the data dir is chowned (what is owned by someone else, e.g. after changing PUID), and the
# walk is skipped when the top folder already belongs to the right user.
DATA="${DATA_DIR:-/data}"
mkdir -p "$DATA"
if [ "$(stat -c '%u:%g' "$DATA")" != "$PUID:$PGID" ] \
    || find "$DATA" -xdev \( -not -user "$PUID" -o -not -group "$PGID" \) -print -quit | grep -q .; then
    chown -R "$PUID:$PGID" "$DATA" 2>/dev/null || echo "warning: cannot chown $DATA" >&2
fi

exec gosu "$PUID:$PGID" "$@"
