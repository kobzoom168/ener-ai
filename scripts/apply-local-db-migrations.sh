#!/usr/bin/env bash
# Apply ener_scan schema on local PostgreSQL (Hetzner / Phase 2A).
# Run on server: bash scripts/apply-local-db-migrations.sh

set -euo pipefail

SRC_ROOT="${1:-/root/ener-scan}"
WORK="/tmp/ener-scan-migrations"
DB="${LOCAL_PG_DB:-ener_scan}"

rm -rf "$WORK"
mkdir -p "$WORK/sql/backup" "$WORK/supabase/migrations" "$WORK/docs/migrations"
cp -a "$SRC_ROOT/sql/000"*.sql "$WORK/sql/" 2>/dev/null || true
cp -a "$SRC_ROOT/sql/backup/"*.sql "$WORK/sql/backup/" 2>/dev/null || true
cp -a "$SRC_ROOT/sql/"[0-9]*.sql "$WORK/sql/" 2>/dev/null || true
cp -a "$SRC_ROOT/docs/migrations/"*.sql "$WORK/docs/migrations/" 2>/dev/null || true
cp -a "$SRC_ROOT/supabase/migrations/"*.sql "$WORK/supabase/migrations/" 2>/dev/null || true
chmod -R a+rX "$WORK"
find "$WORK" -name '*.sql' -exec sed -i 's/\r$//' {} +

run_sql() {
  local f="$1"
  echo "==> $(basename "$f")"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$DB" -f "$f"
}

run_sql "$WORK/sql/000_create_app_users_and_users.sql"

if [[ -f "$WORK/sql/000b_fix_scan_jobs_primary_key.sql" ]]; then
  run_sql "$WORK/sql/000b_fix_scan_jobs_primary_key.sql"
fi

for f in "$WORK"/sql/backup/[0-9]*.sql; do
  base="$(basename "$f")"
  case "$base" in
    013_*|014_*) echo "==> skip sample queries: $base"; continue ;;
  esac
  run_sql "$f"
done

for f in "$WORK"/sql/[0-9]*.sql; do
  run_sql "$f"
done

if [[ -f "$WORK/docs/migrations/scan_image_phashes.sql" ]]; then
  run_sql "$WORK/docs/migrations/scan_image_phashes.sql"
fi

for f in "$WORK"/supabase/migrations/*.sql; do
  run_sql "$f"
done

echo "==> grants for web_anon"
sudo -u postgres psql -d "$DB" <<'EOF'
GRANT USAGE ON SCHEMA public TO web_anon;
GRANT ALL ON ALL TABLES IN SCHEMA public TO web_anon;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO web_anon;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO web_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO web_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO web_anon;
EOF

echo "==> table count"
sudo -u postgres psql -d "$DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
