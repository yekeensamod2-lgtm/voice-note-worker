# Voice note worker (GitHub-hosted Actions + Voicebox/Qwen)

This repository is a public, code-only worker. A GitHub-hosted Ubuntu runner starts on demand, runs Voicebox with Qwen3-TTS 1.7B for higher voice-cloning quality, and returns generated Ogg/Opus audio through a private bridge. Voicebox is never installed on the bot PC.

## Data boundary

- The workflow input is only an opaque UUID. It never contains message text, profile audio, or generated audio.
- The worker authenticates to the bridge with a GitHub Actions OIDC token. The bridge must validate GitHub's JWT signature and require this repository, `workflow_dispatch`, the default branch, and this workflow file.
- The bridge delivers the text and a short-lived URL for the voice reference sample only after OIDC validation. The runner uploads generated audio directly to the bridge.
- Do not upload audio as an Actions artifact, print payloads, or put profile samples in the Actions cache. The only cache is public Qwen model weights and pip package downloads.
- The bridge owns the durable private queue/storage and bot authentication. Its deployment and credentials are separate from this public repository.

## Workflow setup

The `generate-voice.yml` workflow is restricted to manual/API dispatch on `main`, uses a minimal `GITHUB_TOKEN`, and requests an OIDC identity token. The dispatch caller needs a fine-grained token with Actions: write for this repo; ideally use a GitHub App installation token in the private bridge.

Set repository variable `VOICEBOX_BRIDGE_URL` to the HTTPS base URL of the private bridge. The worker fails closed if unset. For voice samples, the authenticated bridge supplies the host alongside its signed URL; the worker requires an exact HTTPS host match. No public host allowlist variable is needed.

The bridge service needs these secrets in its own private host environment, never in this repository:

- Supabase URL and service-role key (private job and object storage)
- bot-to-bridge bearer secret
- GitHub App or fine-grained dispatch credential with Actions: write

The SQL migration creates a private storage bucket and a table with RLS denying public access. Apply it to the existing Supabase project before enabling the bridge.

## Private bridge contract

Bot-side endpoints (Bearer `BOT_BRIDGE_TOKEN`):

- `POST /v1/jobs` multipart fields `text`, `language`, `reference_text`, `voice_sample` (audio). Stores a private job/sample, dispatches the workflow with only the new UUID, returns `{job_id}`.
- `GET /v1/jobs/{job_id}` returns status only.
- `GET /v1/jobs/{job_id}/result` streams Ogg/Opus to the bot once, then deletes job and private sample/result objects.

Runner endpoints (Bearer GitHub OIDC JWT, audience `voicebox-bridge`):

- `POST /v1/jobs/{job_id}/claim` atomically claims one pending job; returns text, language, reference text, and an expiring signed sample URL. Reject repeats and wrong workflow/ref/repository.
- `POST /v1/jobs/{job_id}/complete` multipart `audio` plus `duration_seconds`; persists the private result and marks complete.
- `POST /v1/jobs/{job_id}/fail` JSON `{code: ...}`; accepts only an allowlisted non-sensitive error code.

Never return message text or audio in status responses. Expire abandoned jobs and delete their private objects. Configure the bridge and storage for HTTPS only.

## Limits and expectations

The Qwen 1.7B cache is a performance optimization, not permanent storage; GitHub can evict it. A cache miss downloads roughly 3.5 GB of model weights, with about 6 GB of model memory required. Downloads and caches stay on GitHub-hosted runners; no model or package downloads run on the bot PC. Each Actions run is a fresh CPU runner, so the model must be loaded again each time; there is no warm in-memory model across jobs. The workflow has a 90-minute timeout. The bridge should surface `queued`, `processing`, `ready`, and safe failure states to Telegram.

## Voicebox source

The worker checks out Voicebox v0.6.0 at a pinned commit and uses its official backend API. The install follows Voicebox's upstream backend requirements. Before production, run a private end-to-end benchmark with the intended voice sample and representative short/long text; this public scaffold does not contain a real voice sample or bridge credentials.
