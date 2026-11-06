#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Create (or reuse) a group/user with the requested ids.
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" diskko
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin diskko
fi

# Only the config dir is chowned: /music and /downloads are user data, never touched.
chown "$PUID:$PGID" /config 2>/dev/null || echo "warning: cannot chown /config" >&2

exec gosu "$PUID:$PGID" "$@"
