create table if not exists public.voice_note_profiles (
  profile_id text primary key check (profile_id = 'default'),
  sample_path text not null,
  language text not null check (language in ('en', 'zh', 'ja', 'ko', 'de', 'fr', 'ru', 'pt', 'es', 'it')),
  reference_text text not null check (char_length(reference_text) between 1 and 1000),
  updated_at timestamptz not null default now()
);

create index if not exists voice_note_jobs_sample_path_idx
  on public.voice_note_jobs (sample_path);

alter table public.voice_note_profiles enable row level security;
revoke all on public.voice_note_profiles from anon, authenticated;
grant all on public.voice_note_profiles to service_role;
