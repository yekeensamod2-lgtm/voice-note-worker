-- Preserve the existing single reference as sample 1, then allow up to five
-- private recordings and freeze the selected set onto each queued job.
create table if not exists public.voice_note_profile_samples (
  sample_id uuid primary key default gen_random_uuid(),
  profile_id text not null references public.voice_note_profiles(profile_id) on delete cascade,
  sample_number integer not null check (sample_number between 1 and 5),
  sample_path text not null unique,
  reference_text text not null check (char_length(reference_text) between 1 and 1000),
  created_at timestamptz not null default now(),
  unique (profile_id, sample_number)
);

insert into public.voice_note_profile_samples(profile_id, sample_number, sample_path, reference_text)
select profile_id, 1, sample_path, reference_text
from public.voice_note_profiles
on conflict (profile_id, sample_number) do nothing;

alter table public.voice_note_jobs
  add column if not exists voice_samples jsonb not null default '[]'::jsonb;

create index if not exists voice_note_jobs_voice_samples_idx
  on public.voice_note_jobs using gin (voice_samples);

update public.voice_note_jobs
set voice_samples = jsonb_build_array(jsonb_build_object(
  'sample_path', sample_path,
  'reference_text', reference_text
))
where voice_samples = '[]'::jsonb;

alter table public.voice_note_profile_samples enable row level security;
revoke all on public.voice_note_profile_samples from anon, authenticated;
grant all on public.voice_note_profile_samples to service_role;

create or replace function public.create_voice_note_profile(
  p_sample_path text,
  p_reference_text text,
  p_language text
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.voice_note_profiles(profile_id, sample_path, language, reference_text, updated_at)
  values ('default', p_sample_path, p_language, p_reference_text, now());
  insert into public.voice_note_profile_samples(profile_id, sample_number, sample_path, reference_text)
  values ('default', 1, p_sample_path, p_reference_text);
  return 1;
end;
$$;

create or replace function public.append_voice_note_profile_sample(
  p_sample_path text,
  p_reference_text text
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  current_language text;
  next_number integer;
begin
  select language into current_language
  from public.voice_note_profiles
  where profile_id = 'default'
  for update;
  if current_language is null then
    raise exception 'voice profile is not configured';
  end if;
  select coalesce(max(sample_number), 0) + 1 into next_number
  from public.voice_note_profile_samples
  where profile_id = 'default';
  if next_number > 5 then
    raise exception 'voice profile already has five samples';
  end if;
  insert into public.voice_note_profile_samples(profile_id, sample_number, sample_path, reference_text)
  values ('default', next_number, p_sample_path, p_reference_text);
  update public.voice_note_profiles
  set updated_at = now()
  where profile_id = 'default';
  return next_number;
end;
$$;

revoke all on function public.create_voice_note_profile(text, text, text) from public, anon, authenticated;
revoke all on function public.append_voice_note_profile_sample(text, text) from public, anon, authenticated;
grant execute on function public.create_voice_note_profile(text, text, text) to service_role;
grant execute on function public.append_voice_note_profile_sample(text, text) to service_role;
