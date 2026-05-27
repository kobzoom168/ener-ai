-- Base tables required before sql/backup/002+ (not in staging partial dump).
-- Safe to re-run (IF NOT EXISTS).

create extension if not exists "pgcrypto";

create table if not exists public.app_users (
  id uuid primary key default gen_random_uuid(),
  line_user_id text not null,
  display_name text,
  birthdate text,
  status text not null default 'active',
  paid_until timestamptz,
  paid_remaining_scans integer not null default 0,
  paid_plan_code text,
  free_scan_daily_offset integer not null default 0,
  free_scan_offset_date text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  last_active_at timestamptz
);

create unique index if not exists uq_app_users_line_user_id
  on public.app_users (line_user_id);

create index if not exists idx_app_users_line_user_id
  on public.app_users (line_user_id);

-- LINE user id keyed profile (birthdate overlay; separate from app_users UUID PK).
create table if not exists public.users (
  id text primary key,
  birthdate text,
  updated_at timestamptz
);

-- Legacy/aux entitlement surface (PostgREST exposure + RLS migration).
create table if not exists public.user_entitlements (
  id uuid primary key default gen_random_uuid(),
  app_user_id uuid references public.app_users (id) on delete cascade,
  entitlement_key text,
  metadata jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_user_entitlements_app_user_id
  on public.user_entitlements (app_user_id);
