#!/usr/bin/env bash
# Local dev Postgres cluster — the Docker-free stand-in for
# "Postgres via Docker" from leadpilot-docs/tech-stack/stack-overview.md.
# Isolated from any system Postgres install: its own data directory,
# its own port, trust auth for local socket connections only.
#
# Usage:
#   scripts/devdb.sh init     # first-time setup (idempotent)
#   scripts/devdb.sh start
#   scripts/devdb.sh stop
#   scripts/devdb.sh status
#   scripts/devdb.sh reset    # wipe and reinitialize (destroys local data)
#   scripts/devdb.sh url      # print the DATABASE_URL for .env.local

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA_DIR="$ROOT_DIR/.devdata/pgdata"
SOCK_DIR="$ROOT_DIR/.devdata"
PORT=5433
DB_NAME=leadpilot_dev
DB_USER=leadpilot

# Locate real Postgres binaries — don't assume they're on PATH, since
# multiple Postgres installs may be present on this machine.
PG_BIN=""
for candidate in \
  /Library/PostgreSQL/18/bin \
  /opt/homebrew/opt/postgresql@18/bin \
  /opt/homebrew/opt/postgresql@15/bin \
  /Applications/Postgres.app/Contents/Versions/latest/bin; do
  if [ -x "$candidate/pg_ctl" ]; then
    PG_BIN="$candidate"
    break
  fi
done
if [ -z "$PG_BIN" ]; then
  echo "No Postgres binaries found in any known location. Install Postgres or edit scripts/devdb.sh." >&2
  exit 1
fi

cmd="${1:-}"

case "$cmd" in
  init)
    if [ -d "$PGDATA_DIR" ]; then
      echo "Already initialized at $PGDATA_DIR"
    else
      mkdir -p "$SOCK_DIR"
      "$PG_BIN/initdb" -D "$PGDATA_DIR" -U "$DB_USER" --auth=trust -E UTF8 --locale=en_US.UTF-8 --no-instructions
      echo "Initialized. Run 'scripts/devdb.sh start' next."
    fi
    ;;
  start)
    "$PG_BIN/pg_ctl" -D "$PGDATA_DIR" -l "$SOCK_DIR/pg.log" -o "-p $PORT -k $SOCK_DIR" start
    if ! PGHOST="$SOCK_DIR" PGPORT="$PORT" PGUSER="$DB_USER" "$PG_BIN/psql" -lqt | cut -d '|' -f1 | grep -qw "$DB_NAME"; then
      PGHOST="$SOCK_DIR" PGPORT="$PORT" PGUSER="$DB_USER" "$PG_BIN/createdb" "$DB_NAME"
      echo "Created database $DB_NAME"
    fi
    ;;
  stop)
    "$PG_BIN/pg_ctl" -D "$PGDATA_DIR" stop -m fast
    ;;
  status)
    "$PG_BIN/pg_ctl" -D "$PGDATA_DIR" status
    ;;
  reset)
    read -r -p "This deletes all local dev data in $PGDATA_DIR. Continue? [y/N] " confirm
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
      "$PG_BIN/pg_ctl" -D "$PGDATA_DIR" stop -m fast 2>/dev/null || true
      rm -rf "$PGDATA_DIR"
      echo "Wiped. Run 'scripts/devdb.sh init' then 'start' again."
    else
      echo "Cancelled."
    fi
    ;;
  url)
    echo "postgresql+psycopg://$DB_USER@/$DB_NAME?host=$SOCK_DIR&port=$PORT"
    ;;
  *)
    echo "Usage: scripts/devdb.sh {init|start|stop|status|reset|url}" >&2
    exit 1
    ;;
esac
