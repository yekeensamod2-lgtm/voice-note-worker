create table if not exists public.voice_note_jobs (
  id uuid primary key,
  status text not null check (status in ('pending', 'processing', 'completed', 'failed')),
  text text not null check (char_length(text) between 1 and 12000),
  language text not null check (language in ('en', 'zh', 'ja', 'ko', 'de', 'fr', 'ru', 'pt', 'es', 'it')),
  reference_text text not null check (char_length(reference_text) between 1 and 1000),
  sample_path text not null,
  result_path text,
  run_id text,
  duration_seconds double precision,
  error_code text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  expires_at timestamptz not null
);

create index if not exists voice_note_jobs_expiry_idx
  on public.voice_note_jobs (expires_at);

alter table public.voice_note_jobs enable row level security;
revoke all on public.voice_note_jobs from anon, authenticated;
grant all on public.voice_note_jobs to service_role;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'voicebox-private',
  'voicebox-private',
  false,
  125829120,
  array['audio/wav', 'audio/mpeg', 'audio/mp4', 'audio/ogg', 'audio/opus', 'audio/webm', 'audio/flac', 'audio/aac', 'application/octet-stream', 'application/zip']
)
on conflict (id) do update
set public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

create or replace function public.claim_voice_note_job(p_job_id uuid, p_run_id text)
returns setof public.voice_note_jobs
language sql
security definer
set search_path = public
as $$
  update public.voice_note_jobs
     set status = 'processing',
         run_id = p_run_id,
         started_at = now(),
         expires_at = now() + interval '2 hours'
   where id = p_job_id
     and (status = 'pending' or (status = 'processing' and run_id = p_run_id))
     and expires_at > now()
  returning *;
$$;

revoke all on function public.claim_voice_note_job(uuid, text) from public, anon, authenticated;
grant execute on function public.claim_voice_note_job(uuid, text) to service_role;
