#!/bin/sh
# Make /app/data writable, then drop out of root before running anything.
#
# `../data` is a bind mount, so whatever the image did to /app/data at build
# time is replaced by the host directory and its ownership. Three things then
# go wrong on their own, and all three have actually happened:
#
#   - the host directory belongs to the user who cloned the repo, not to the
#     image's uid, so the app cannot write and dies on its first mkdir;
#   - the directory does not exist at all -- `data/` is gitignored, so a fresh
#     clone has none -- and Docker creates the missing path as root:root;
#   - Docker Desktop hides both by mapping ownership, so it works on a Mac and
#     fails on the Linux box you deploy to.
#
# Asking the operator to line up ids by hand fixes it exactly as often as they
# remember to. This adopts the directory's own owner instead: files come out
# belonging to whoever owns ./data on the host, which is the point of it being
# a plain directory you can open, back up and delete.
set -e

DATA=/app/data
FALLBACK_UID=10001

if [ "$(id -u)" != "0" ]; then
    # Already unprivileged -- someone set `user:` in compose, or this is a
    # hardened runtime. Nothing to adjust and no way to adjust it; if the
    # directory is unwritable the app's own error is the honest one.
    exec "$@"
fi

uid="${REELFORGE_UID:-}"
gid="${REELFORGE_GID:-}"

if [ -z "$uid" ] && [ -d "$DATA" ]; then
    # Adopt the bind mount's owner. Skip root: that means Docker created the
    # directory because the host had none, and running the app as root would
    # leave every reel root-owned on the host.
    owner="$(stat -c %u "$DATA" 2>/dev/null || echo 0)"
    if [ "$owner" != "0" ]; then
        uid="$owner"
        gid="$(stat -c %g "$DATA" 2>/dev/null || echo "$owner")"
    fi
fi

uid="${uid:-$FALLBACK_UID}"
gid="${gid:-$uid}"

mkdir -p "$DATA/jobs"
# Only when it is actually wrong: a chown -R over a large data directory on
# every start is slow, and on a bind mount it is slow enough to notice.
if [ "$(stat -c %u "$DATA")" != "$uid" ] || [ "$(stat -c %u "$DATA/jobs")" != "$uid" ]; then
    chown -R "$uid:$gid" "$DATA" || echo "entrypoint: could not chown $DATA; continuing" >&2
fi

exec setpriv --reuid="$uid" --regid="$gid" --clear-groups -- "$@"
