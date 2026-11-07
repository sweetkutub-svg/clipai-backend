-- Run in Supabase -> SQL Editor
create table if not exists public.clips (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  job_id text,
  b2_key text,
  caption text,
  reason text,
  created_at timestamptz not null default now()
);
create index if not exists clips_user_idx on public.clips(user_id, created_at desc);

create table if not exists public.yt_tokens (
  user_id uuid primary key,
  token text,
  refresh_token text,
  scopes jsonb,
  updated_at timestamptz default now()
);

-- Backend uses the service_role key; lock tables for everyone else
alter table public.clips enable row level security;
alter table public.yt_tokens enable row level security;
