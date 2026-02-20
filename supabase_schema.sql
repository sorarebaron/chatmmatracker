-- ChatMMA Tracker — Supabase Schema
-- Run this entire script in your Supabase SQL Editor (Project > SQL Editor > New query)

-- Enable UUID generation
create extension if not exists "uuid-ossp";

-- ─────────────────────────────────────────
-- events
-- ─────────────────────────────────────────
create table events (
  event_id   uuid primary key default uuid_generate_v4(),
  name       text not null,
  date       date,
  location   text,
  promotion  text,
  created_at timestamptz default now()
);
alter table events enable row level security;

-- ─────────────────────────────────────────
-- fights
-- ─────────────────────────────────────────
create table fights (
  fight_id    uuid primary key default uuid_generate_v4(),
  event_id    uuid references events(event_id) on delete cascade,
  fighter_a   text not null,
  fighter_b   text not null,
  weight_class text,
  bout_order  int,
  title_fight bool default false,
  status      text default 'scheduled'
              check (status in ('scheduled', 'completed', 'cancelled'))
);
alter table fights enable row level security;

-- ─────────────────────────────────────────
-- fighter_aliases
-- ─────────────────────────────────────────
create table fighter_aliases (
  alias_id       uuid primary key default uuid_generate_v4(),
  canonical_name text not null,
  alias          text not null unique
);
alter table fighter_aliases enable row level security;

-- ─────────────────────────────────────────
-- analyst_picks
-- ─────────────────────────────────────────
create table analyst_picks (
  pick_id          uuid primary key default uuid_generate_v4(),
  fight_id         uuid references fights(fight_id) on delete cascade,
  analyst_name     text not null,
  source_url       text,
  picked_fighter   text,
  method_prediction text,
  confidence_tag   text check (confidence_tag in ('lean', 'confident', 'lock')),
  reasoning_notes  text,
  created_at       timestamptz default now()
);
alter table analyst_picks enable row level security;

-- ─────────────────────────────────────────
-- pick_tags
-- ─────────────────────────────────────────
create table pick_tags (
  tag_id  uuid primary key default uuid_generate_v4(),
  pick_id uuid references analyst_picks(pick_id) on delete cascade,
  tag     text not null
);
alter table pick_tags enable row level security;

-- ─────────────────────────────────────────
-- results
-- ─────────────────────────────────────────
create table results (
  result_id   uuid primary key default uuid_generate_v4(),
  fight_id    uuid references fights(fight_id) on delete cascade unique,
  winner      text,
  method      text check (method in ('KO/TKO', 'Submission', 'Decision', 'NC', 'DQ')),
  round       int,
  time        text,
  referee     text,
  judge1_name  text,
  judge1_score text,
  judge2_name  text,
  judge2_score text,
  judge3_name  text,
  judge3_score text
);
alter table results enable row level security;

-- ─────────────────────────────────────────
-- NOTE ON SECURITY
-- RLS is enabled on all tables above.
-- No public (anon) policies are created, so anonymous access is blocked.
-- The app uses the service_role key exclusively, which bypasses RLS server-side.
-- Never expose the service_role key to the browser or public clients.
-- ─────────────────────────────────────────
