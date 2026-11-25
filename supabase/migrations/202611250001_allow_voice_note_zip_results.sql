do $$
begin
  if not exists (
    select 1 from storage.buckets where id = 'voicebox-private'
  ) then
    raise exception 'Required Storage bucket voicebox-private does not exist';
  end if;

  update storage.buckets
     set allowed_mime_types = case
       when allowed_mime_types is null then null
       when 'application/zip' = any(allowed_mime_types) then allowed_mime_types
       else array_append(allowed_mime_types, 'application/zip')
     end
   where id = 'voicebox-private';
end
$$;
