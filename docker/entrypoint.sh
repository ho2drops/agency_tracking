#!/bin/bash
# Startup entrypoint for agency_tracking on Railway
#
# Persistence note: Railway container instances are ephemeral.
# A Volume MUST be mounted at $BENCH_PATH/sites (/home/frappe/bench/sites)
# so site_config.json, encryption keys, and uploaded media survive redeployments.

set -euo pipefail

echo "==> Initializing Agency Tracking service environment..."

# -----------------------------------------------------------------------------
# 1. Database & Environment Auto-Discovery
# -----------------------------------------------------------------------------

# Parse connection string (DATABASE_URL / MYSQL_URL / MARIADB_URL) if provided
DATABASE_CONN_URL="${DATABASE_URL:-${MYSQL_URL:-${MARIADB_URL:-${MYSQL_PRIVATE_URL:-${MARIADB_PRIVATE_URL:-}}}}}"
if [ -n "$DATABASE_CONN_URL" ] && [ -z "${DB_HOST:-}" ]; then
  echo "Parsing database connection URL..."
  PROTO="$(echo "$DATABASE_CONN_URL" | sed -e's,^\(.*://\).*,\1,g')"
  URL_NOPROTO="${DATABASE_CONN_URL#"$PROTO"}"
  USER_PASS="$(echo "$URL_NOPROTO" | grep @ | cut -d@ -f1 || true)"
  HOST_PORT_DB="${URL_NOPROTO#"$USER_PASS"}"
  HOST_PORT_DB="${HOST_PORT_DB#@}"
  
  if [ -n "$USER_PASS" ]; then
    DB_USER="${DB_USER:-$(echo "$USER_PASS" | cut -d: -f1)}"
    DB_PASSWORD="${DB_PASSWORD:-$(echo "$USER_PASS" | cut -d: -f2-)}"
  fi
  
  HOST_PORT="$(echo "$HOST_PORT_DB" | cut -d/ -f1)"
  DB_NAME="${DB_NAME:-$(echo "$HOST_PORT_DB" | cut -d/ -f2- | cut -d? -f1)}"
  DB_HOST="${DB_HOST:-$(echo "$HOST_PORT" | cut -d: -f1)}"
  if [[ "$HOST_PORT" == *:* ]]; then
    DB_PORT="${DB_PORT:-$(echo "$HOST_PORT" | cut -d: -f2)}"
  fi
fi

# Fallback to standard Railway environment variables (MySQL & MariaDB variants)
DB_HOST="${DB_HOST:-${MYSQLHOST:-${MARIADBHOST:-${MARIADB_HOST:-${MYSQL_HOST:-}}}}}"
DB_PORT="${DB_PORT:-${MYSQLPORT:-${MARIADBPORT:-${MARIADB_PORT:-${MYSQL_PORT:-3306}}}}}"
DB_USER="${DB_USER:-${MYSQLUSER:-${MARIADBUSER:-${MARIADB_USER:-${MYSQL_USER:-root}}}}}"
DB_PASSWORD="${DB_PASSWORD:-${MYSQLPASSWORD:-${MARIADBPASSWORD:-${MARIADB_PASSWORD:-${MYSQL_PASSWORD:-${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}}}}}}"
DB_NAME="${DB_NAME:-${MYSQLDATABASE:-${MARIADBDATABASE:-${MARIADB_DATABASE:-${MYSQL_DATABASE:-agency_tracking}}}}}"

# Root DBA credentials for bench new-site (creating databases & granting privileges)
DB_ROOT_USER="${DB_ROOT_USER:-${MYSQLROOTUSER:-${MARIADBROOTUSER:-${MYSQL_ROOT_USER:-${MARIADB_ROOT_USER:-root}}}}}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-${MYSQLROOTPASSWORD:-${MARIADBROOTPASSWORD:-${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-${DB_PASSWORD:-}}}}}}"

# Auto-detect public site name or domain
SITE_NAME="${SITE_NAME:-${RAILWAY_PUBLIC_DOMAIN:-${RAILWAY_STATIC_URL:-agency-tracking.local}}}"
SITE_NAME="${SITE_NAME#http://}"
SITE_NAME="${SITE_NAME#https://}"
SITE_NAME="${SITE_NAME%%/*}"

ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"

export PORT="${PORT:-8080}"
export SITES_PATH="/home/frappe/bench/sites"
export GUNICORN_WORKERS="${GUNICORN_WORKERS:-4}"
# gthread workers (not sync): PDF generation (wkhtmltopdf) and document parsing (tesseract/
# PyMuPDF) all shell out to a subprocess and block waiting on it -- that blocking call releases
# the GIL, so extra threads within the same worker process can keep serving other requests
# while one thread sits blocked on a slow subprocess, instead of that request fully occupying
# an entire worker (see raw.md-adjacent perf notes: this was the root cause of "N slow PDF/OCR
# requests starve the whole site" under concurrent load, with only 4 sync workers total before).
export GUNICORN_THREADS="${GUNICORN_THREADS:-4}"
export GUNICORN_BIND_ARGS="--bind 0.0.0.0:${PORT}"
if [ "$PORT" != "8000" ]; then
  export GUNICORN_BIND_ARGS="${GUNICORN_BIND_ARGS} --bind 0.0.0.0:8000"
fi

echo "Site Target Domain : $SITE_NAME"
echo "Database Target    : $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME (Root: $DB_ROOT_USER)"
echo "HTTP Port          : $PORT"
echo "Gunicorn Workers   : $GUNICORN_WORKERS"
echo "Gunicorn Threads   : $GUNICORN_THREADS"
echo "Gunicorn Bind Args : $GUNICORN_BIND_ARGS"

if [ -z "$DB_HOST" ]; then
  echo "ERROR: DB_HOST (or MYSQLHOST / DATABASE_URL) is missing. Provision and link a MySQL/MariaDB plugin in Railway." >&2
  exit 1
fi

cd "$BENCH_PATH"

# -----------------------------------------------------------------------------
# 2. Volume Initialization & Seeding
# -----------------------------------------------------------------------------

# If sites volume is brand new or empty, restore baseline files from image backup template
if [ ! -f "sites/apps.txt" ]; then
  echo "Seeding baseline sites configuration from image template..."
  cp -a /home/frappe/sites-init/. sites/
fi

# Guarantee agency_tracking is listed in apps.txt
if ! grep -q "^agency_tracking$" sites/apps.txt 2>/dev/null; then
  echo "agency_tracking" >> sites/apps.txt
fi

# Always sync static assets from build-time image into volume
mkdir -p sites/assets
if [ -d "/home/frappe/sites-init/assets" ]; then
  echo "Syncing latest built assets into sites/assets..."
  cp -a /home/frappe/sites-init/assets/. sites/assets/
fi

chown -R frappe:frappe sites

# -----------------------------------------------------------------------------
# 3. Database Connectivity Check
# -----------------------------------------------------------------------------

echo "Verifying MariaDB/MySQL connection at ${DB_HOST}:${DB_PORT}..."
MAX_RETRIES=60
RETRY_COUNT=0
until mysqladmin ping -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" --silent 2>/dev/null || [ $RETRY_COUNT -eq $MAX_RETRIES ]; do
  sleep 2
  RETRY_COUNT=$((RETRY_COUNT + 1))
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
  echo "ERROR: Unable to reach database server at ${DB_HOST}:${DB_PORT} after ${MAX_RETRIES} attempts." >&2
  exit 1
fi
echo "Database server is responsive."

# -----------------------------------------------------------------------------
# 4. Start Temporary Redis Daemon for Initialization
# -----------------------------------------------------------------------------

echo "Starting temporary Redis daemon for site initialization..."
redis-server --daemonize yes --port 6379 --save ""
sleep 1

REDIS_SERVER_URL="${REDIS_URL:-redis://127.0.0.1:6379}"

bench set-config -g redis_cache "$REDIS_SERVER_URL"
bench set-config -g redis_queue "$REDIS_SERVER_URL"
bench set-config -g redis_socketio "$REDIS_SERVER_URL"
bench set-config -g webserver_port "$PORT"
bench set-config -g default_site "$SITE_NAME"
bench set-config -g serve_default_site true
# Uploads: this app allows 20 MB (agency_tracking/upload_guard.py); the framework default is 10 MB.
bench set-config -g max_file_size 20971520
echo "$SITE_NAME" > sites/currentsite.txt

# -----------------------------------------------------------------------------
# 5. Site Creation or Schema Migration
# -----------------------------------------------------------------------------

# Check if database contains base Frappe tables
HAS_BASE_TABLES=false
if mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" -e "DESCRIBE tabDocType;" >/dev/null 2>&1; then
  HAS_BASE_TABLES=true
fi

# If database already has tables, look for an existing site configuration
EXISTING_SITE=""
if [ "$HAS_BASE_TABLES" = true ]; then
  EXISTING_SITE=$(find sites -maxdepth 2 -name "site_config.json" -not -path "sites/assets/*" 2>/dev/null | head -n 1 | cut -d/ -f2 || true)
fi

if [ -n "$EXISTING_SITE" ] && [ ! -e "sites/${SITE_NAME}" ]; then
  echo "Found existing site '${EXISTING_SITE}'. Linking '${SITE_NAME}' -> '${EXISTING_SITE}'..."
  ln -sfn "$EXISTING_SITE" "sites/${SITE_NAME}"
fi

if [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ] && [ ! -e "sites/${RAILWAY_PUBLIC_DOMAIN}" ]; then
  TARGET_SITE="${EXISTING_SITE:-${SITE_NAME}}"
  echo "Linking Railway public domain '${RAILWAY_PUBLIC_DOMAIN}' -> '${TARGET_SITE}'..."
  ln -sfn "$TARGET_SITE" "sites/${RAILWAY_PUBLIC_DOMAIN}"
fi

if [ "${FORCE_NEW_SITE:-0}" = "1" ] || ([ "$HAS_BASE_TABLES" = false ] && [ -z "$EXISTING_SITE" ]); then
  if [ -d "sites/${SITE_NAME}" ]; then
    echo "Database ${DB_NAME} is clean or FORCE_NEW_SITE=1 set. Clearing site folder for fresh installation..."
    rm -rf "sites/${SITE_NAME}"
  fi
fi

if [ ! -f "sites/${SITE_NAME}/site_config.json" ]; then
  echo "No existing site configuration found for ${SITE_NAME}. Creating new Frappe site..."
  bench new-site "$SITE_NAME" \
    --db-type mariadb \
    --db-host "$DB_HOST" \
    --db-port "$DB_PORT" \
    --db-name "$DB_NAME" \
    --db-root-username "$DB_ROOT_USER" \
    --db-root-password "$DB_ROOT_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD" \
    --mariadb-user-host-login-scope '%' \
    --install-app agency_tracking \
    --force \
    --set-default
else
  echo "Existing site_config.json found for ${SITE_NAME}. Syncing environment settings and running migrations..."
  python3 -c "
import json, os
p = f'sites/{os.environ.get(\"SITE_NAME\")}/site_config.json'
try:
    if os.path.exists(p):
        with open(p, 'r') as f:
            data = json.load(f)
        changed = False
        if os.environ.get('DB_HOST') and data.get('db_host') != os.environ.get('DB_HOST'):
            data['db_host'] = os.environ.get('DB_HOST')
            changed = True
        if os.environ.get('DB_PORT') and str(data.get('db_port')) != str(os.environ.get('DB_PORT')):
            data['db_port'] = int(os.environ.get('DB_PORT'))
            changed = True
        if os.environ.get('DB_PASSWORD') and data.get('db_password') != os.environ.get('DB_PASSWORD'):
            data['db_password'] = os.environ.get('DB_PASSWORD')
            changed = True
        if changed:
            with open(p, 'w') as f:
                json.dump(data, f, indent=1)
            print('Successfully updated site_config.json with latest DB credentials.')
except Exception as e:
    print('Warning: Failed syncing site_config.json:', e)
"
  bench --site "$SITE_NAME" migrate
  bench use "$SITE_NAME"
fi

# Ensure site is configured as the default site for all incoming requests
bench set-config -g default_site "$SITE_NAME"
bench set-config -g serve_default_site true
bench --site "$SITE_NAME" set-config host_name "https://${SITE_NAME}"

# Passport OCR: OCR.space API key (see agency_tracking/passport_parser.py's
# `_ocrspace_full_text` -- reads this back via frappe.conf.get("ocrspace_api_key")). Optional --
# passport parsing degrades gracefully to the self-hosted Tesseract pipeline if unset.
if [ -n "${OCRSPACE_API_KEY:-}" ]; then
  bench --site "$SITE_NAME" set-config ocrspace_api_key "$OCRSPACE_API_KEY"
fi

echo "$SITE_NAME" > sites/currentsite.txt

chown -R frappe:frappe sites

# -----------------------------------------------------------------------------
# 6. DB User Host Scope Self-Healing
# -----------------------------------------------------------------------------

if [ -f "sites/${SITE_NAME}/site_config.json" ]; then
  SITE_DB_USER=$(python3 -c "import json; print(json.load(open('sites/${SITE_NAME}/site_config.json')).get('db_name', ''))" 2>/dev/null || true)
  if [ -n "$SITE_DB_USER" ]; then
    STALE_HOSTS=$(mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" -N -B \
      -e "SELECT host FROM mysql.user WHERE user = '${SITE_DB_USER}' AND host != '%';" 2>/dev/null || true)
    if [ -n "$STALE_HOSTS" ]; then
      echo "Widen DB user scope for ${SITE_DB_USER}@'%'..."
      while IFS= read -r stale_host; do
        [ -z "$stale_host" ] && continue
        mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" \
          -e "RENAME USER '${SITE_DB_USER}'@'${stale_host}' TO '${SITE_DB_USER}'@'%';" 2>/dev/null || \
        mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" \
          -e "DROP USER IF EXISTS '${SITE_DB_USER}'@'${stale_host}';" 2>/dev/null || true
      done <<< "$STALE_HOSTS"
      mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" \
        -e "GRANT ALL PRIVILEGES ON \`${SITE_DB_USER}\`.* TO '${SITE_DB_USER}'@'%'; FLUSH PRIVILEGES;" 2>/dev/null || true
    fi
  fi
fi

# Stop temporary Redis before supervisord process manager takes over
echo "Stopping temporary initialization Redis..."
redis-cli shutdown 2>/dev/null || true
sleep 1

echo "==> Initialization complete. Starting services under supervisord..."
exec "$@"

