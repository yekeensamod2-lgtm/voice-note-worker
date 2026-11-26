from __future__ import annotations

import hmac
import io
import os
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import quote, urlparse

import jwt
import requests
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from jwt import PyJWKClient

app = FastAPI(title="Private voice-note bridge", docs_url=None, redoc_url=None, openapi_url=None)
BUCKET = "voicebox-private"
MAX_TEXT_CHARS = 12_000
MAX_REFERENCE_CHARS = 1_000
MAX_SAMPLE_BYTES = 25 * 1024 * 1024
MAX_PROFILE_SAMPLES = 5
MAX_OUTPUT_BYTES = 120 * 1024 * 1024
JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
OIDC_AUDIENCE = "voicebox-bridge"
JWKS = PyJWKClient("https://token.actions.githubusercontent.com/.well-known/jwks")


def setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise HTTPException(status_code=503, detail="bridge_not_configured")
    return value


def bot_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = setting("BOT_BRIDGE_TOKEN")
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def supabase_request(method: str, path: str, **kwargs) -> requests.Response:
    base = setting("SUPABASE_URL").rstrip("/")
    key = setting("SUPABASE_SERVICE_ROLE_KEY")
    headers = kwargs.pop("headers", {})
    headers.update({"apikey": key, "Authorization": f"Bearer {key}"})
    response = requests.request(method, f"{base}{path}", headers=headers, timeout=(10, 60), **kwargs)
    if response.status_code >= 400:
        raise RuntimeError("storage_or_database_error")
    return response


def table(path: str, **kwargs) -> requests.Response:
    return supabase_request("GET", f"/rest/v1/voice_note_jobs?{path}", **kwargs)


def storage_path(object_path: str) -> str:
    return f"/storage/v1/object/{BUCKET}/{quote(object_path, safe='/')}"


def storage_upload(object_path: str, data: bytes, content_type: str) -> None:
    supabase_request(
        "POST",
        storage_path(object_path),
        data=data,
        headers={"Content-Type": content_type, "x-upsert": "false"},
    )


def storage_download(object_path: str) -> bytes:
    response = supabase_request("GET", storage_path(object_path))
    if len(response.content) > MAX_OUTPUT_BYTES:
        raise RuntimeError("stored_audio_too_large")
    return response.content


def storage_delete(paths: list[str]) -> None:
    if paths:
        supabase_request(
            "DELETE",
            f"/storage/v1/object/{BUCKET}",
            json={"prefixes": paths},
        )


def delete_job(job: dict) -> None:
    # Reusable profile samples live under profiles/ and are intentionally kept.
    # Legacy per-job samples live under samples/ and expire with their job.
    sample = str(job.get("sample_path") or "")
    voice_samples = job.get("voice_samples") or []
    profile_samples = [str(item.get("sample_path") or "") for item in voice_samples if isinstance(item, dict)] if isinstance(voice_samples, list) else []
    paths = ([sample] if sample.startswith("samples/") else []) + [job.get("result_path")]
    storage_delete([path for path in paths if path])
    supabase_request("DELETE", f"/rest/v1/voice_note_jobs?id=eq.{job['id']}")
    for path in set(profile_samples + ([sample] if sample.startswith("profiles/") else [])):
        if not path.startswith("profiles/"):
            continue
        encoded = quote(path, safe="")
        current = supabase_request("GET", f"/rest/v1/voice_note_profile_samples?sample_path=eq.{encoded}&select=sample_id&limit=1").json()
        contains = quote('[{"sample_path":"' + path + '"}]', safe="")
        active = table(f"or=(sample_path.eq.{encoded},voice_samples.cs.{contains})&select=id&limit=1").json()
        if not current and not active:
            storage_delete([path])


def purge_expired() -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = table(f"expires_at=lt.{quote(now, safe=':.-')}&select=id,sample_path,result_path,voice_samples")
    for row in rows.json():
        try:
            delete_job(row)
        except Exception:
            # Keep the expired row for another cleanup attempt; never log payloads or object URLs.
            continue


def bot_job(job_id: str) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="job_not_found")
    rows = table(f"id=eq.{job_id}&select=id,status,expires_at,duration_seconds,result_path,sample_path,reference_text,voice_samples")
    found = rows.json()
    if not found:
        raise HTTPException(status_code=404, detail="job_not_found")
    return found[0]


def current_voice_profile() -> dict | None:
    response = supabase_request(
        "GET", "/rest/v1/voice_note_profiles?profile_id=eq.default&select=profile_id,sample_path,language,reference_text,updated_at&limit=1"
    )
    rows = response.json()
    return rows[0] if rows else None


def voice_profile_samples() -> list[dict]:
    response = supabase_request(
        "GET", "/rest/v1/voice_note_profile_samples?profile_id=eq.default&select=sample_id,sample_number,sample_path,reference_text&order=sample_number.asc"
    )
    return response.json()


def validate_profile_sample(sample: bytes, filename: str, language: str, reference_text: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension not in {"wav", "mp3", "m4a", "ogg", "opus", "webm", "flac", "aac"}:
        raise HTTPException(status_code=400, detail="unsupported_audio")
    if not sample or len(sample) > MAX_SAMPLE_BYTES:
        raise HTTPException(status_code=413, detail="sample_too_large")
    if language not in {"en", "zh", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"}:
        raise HTTPException(status_code=400, detail="unsupported_language")
    if not reference_text.strip() or len(reference_text) > MAX_REFERENCE_CHARS:
        raise HTTPException(status_code=400, detail="invalid_reference_text")

    return extension


def store_profile_sample(sample: bytes, filename: str, content_type: str, language: str, reference_text: str, *, first: bool) -> int:
    extension = validate_profile_sample(sample, filename, language, reference_text)
    profile = current_voice_profile()
    if first and profile:
        raise HTTPException(status_code=409, detail="voice_profile_already_exists_use_add_sample")
    if not first and not profile:
        raise HTTPException(status_code=409, detail="voice_profile_not_configured")
    if not first and language != str(profile["language"]):
        raise HTTPException(status_code=400, detail="sample_language_mismatch")
    if not first and len(voice_profile_samples()) >= MAX_PROFILE_SAMPLES:
        raise HTTPException(status_code=409, detail="voice_profile_sample_limit_reached")
    object_path = f"profiles/default-{uuid.uuid4()}.{extension}"
    storage_upload(object_path, sample, content_type or "application/octet-stream")
    try:
        rpc = "create_voice_note_profile" if first else "append_voice_note_profile_sample"
        arguments = {"p_sample_path": object_path, "p_reference_text": reference_text.strip()}
        if first:
            arguments["p_language"] = language
        result = supabase_request("POST", f"/rest/v1/rpc/{rpc}", json=arguments).json()
    except Exception:
        storage_delete([object_path])
        raise
    if isinstance(result, list):
        result = result[0] if result else None
    if isinstance(result, dict):
        result = result.get("sample_number")
    return int(result or (1 if first else len(voice_profile_samples())))


def worker_claims(authorization: Annotated[str | None, Header()] = None) -> dict:
    token = authorization.removeprefix("Bearer ") if authorization else ""
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        signing_key = JWKS.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=OIDC_AUDIENCE,
            issuer="https://token.actions.githubusercontent.com",
            options={"require": ["exp", "iat", "repository", "ref", "event_name", "workflow_ref", "run_id"]},
        )
    except Exception:
        raise HTTPException(status_code=401, detail="unauthorized") from None

    repo = setting("GITHUB_REPOSITORY")
    expected_workflow = f"{repo}/.github/workflows/generate-voice.yml@refs/heads/main"
    if (
        claims.get("repository") != repo
        or claims.get("ref") != "refs/heads/main"
        or claims.get("event_name") != "workflow_dispatch"
        or claims.get("workflow_ref") != expected_workflow
    ):
        raise HTTPException(status_code=403, detail="workflow_not_allowed")
    return claims


def require_job_run(job_id: str, claims: dict) -> dict:
    job = bot_job(job_id)
    if job.get("status") != "processing":
        raise HTTPException(status_code=409, detail="job_not_processing")
    rows = table(f"id=eq.{job_id}&select=run_id")
    found = rows.json()
    if not found or str(found[0].get("run_id")) != str(claims.get("run_id")):
        raise HTTPException(status_code=403, detail="run_not_owner")
    return job


def signed_sample_url(object_path: str) -> str:
    response = supabase_request(
        "POST",
        f"/storage/v1/object/sign/{BUCKET}/{quote(object_path, safe='/')}",
        # All five samples are signed together. Give the runner enough time to
        # download and upload the complete set while keeping URLs short-lived.
        json={"expiresIn": 900},
    )
    signed = response.json().get("signedURL")
    if not isinstance(signed, str):
        raise RuntimeError("signed_url_error")
    if signed.startswith("/storage/v1/"):
        signed = setting("SUPABASE_URL").rstrip("/") + signed
    elif signed.startswith("/"):
        # Storage API returns a path relative to its own base (/object/sign/...),
        # while the project URL stops before /storage/v1.
        signed = setting("SUPABASE_URL").rstrip("/") + "/storage/v1" + signed
    parsed = urlparse(signed)
    if parsed.scheme != "https" or parsed.hostname != urlparse(setting("SUPABASE_URL")).hostname:
        raise RuntimeError("signed_url_error")
    return signed


def dispatch(job_id: str) -> None:
    repo = setting("GITHUB_REPOSITORY")
    workflow = setting("GITHUB_WORKFLOW_FILE")
    token = setting("GITHUB_DISPATCH_TOKEN")
    ref = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
    response = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": ref, "inputs": {"job_id": job_id}},
        timeout=(10, 30),
    )
    if response.status_code != 204:
        raise RuntimeError("dispatch_failed")


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "voice-note-bridge"}


@app.get("/v1/profile", dependencies=[Depends(bot_auth)])
def get_profile_status():
    profile = current_voice_profile()
    if not profile:
        return {"configured": False, "sample_count": 0, "max_samples": MAX_PROFILE_SAMPLES}
    count = len(voice_profile_samples())
    return {
        "configured": True,
        "language": profile["language"],
        "updated_at": profile["updated_at"],
        "sample_count": count,
        "max_samples": MAX_PROFILE_SAMPLES,
    }


@app.put("/v1/profile", dependencies=[Depends(bot_auth)])
async def save_profile(
    voice_sample: Annotated[UploadFile, File()],
    reference_text: Annotated[str, Form()],
    language: Annotated[str, Form()] = "en",
):
    sample = await voice_sample.read(MAX_SAMPLE_BYTES + 1)
    sample_number = store_profile_sample(
        sample,
        voice_sample.filename or "voice-sample.ogg",
        voice_sample.content_type or "application/octet-stream",
        language,
        reference_text,
        first=True,
    )
    return {"configured": True, "language": language, "sample_number": sample_number, "sample_count": sample_number}


@app.post("/v1/profile/samples", dependencies=[Depends(bot_auth)])
async def add_profile_sample(
    voice_sample: Annotated[UploadFile, File()],
    reference_text: Annotated[str, Form()],
    language: Annotated[str, Form()] = "en",
):
    sample = await voice_sample.read(MAX_SAMPLE_BYTES + 1)
    profile = current_voice_profile()
    selected_language = str(profile["language"]) if profile else language
    sample_number = store_profile_sample(
        sample,
        voice_sample.filename or "voice-sample.ogg",
        voice_sample.content_type or "application/octet-stream",
        selected_language,
        reference_text,
        first=False,
    )
    return {"configured": True, "language": selected_language, "sample_number": sample_number, "sample_count": sample_number}


@app.delete("/v1/profile", dependencies=[Depends(bot_auth)])
def delete_profile():
    profile = current_voice_profile()
    if not profile:
        return {"configured": False}
    samples = voice_profile_samples()
    supabase_request("DELETE", "/rest/v1/voice_note_profiles?profile_id=eq.default")
    for item in samples:
        path = str(item.get("sample_path") or "")
        encoded = quote(path, safe="")
        contains = quote('[{"sample_path":"' + path + '"}]', safe="")
        active = table(f"or=(sample_path.eq.{encoded},voice_samples.cs.{contains})&select=id&limit=1").json()
        if path and not active:
            storage_delete([path])
    return {"configured": False}


@app.post("/v1/jobs", dependencies=[Depends(bot_auth)])
async def create_job(
    text: Annotated[str, Form()],
    language: Annotated[str, Form()] = "",
):
    if not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=400, detail="invalid_text")
    profile = current_voice_profile()
    if not profile:
        raise HTTPException(status_code=409, detail="voice_profile_not_configured")
    selected_language = language.strip() or str(profile["language"])
    if selected_language not in {"en", "zh", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"}:
        raise HTTPException(status_code=400, detail="unsupported_language")

    purge_expired()
    job_id = str(uuid.uuid4())
    profile_samples = voice_profile_samples()
    if not 1 <= len(profile_samples) <= MAX_PROFILE_SAMPLES:
        raise HTTPException(status_code=409, detail="voice_profile_samples_invalid")
    sample_path = str(profile_samples[0]["sample_path"])
    voice_samples = [{"sample_path": str(item["sample_path"]), "reference_text": str(item["reference_text"])} for item in profile_samples]
    now = datetime.now(timezone.utc)
    record = {
        "id": job_id,
        "status": "pending",
        "text": text,
        "language": selected_language,
        "reference_text": profile["reference_text"],
        "sample_path": sample_path,
        "voice_samples": voice_samples,
        "result_path": None,
        "run_id": None,
        "duration_seconds": None,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
    }
    try:
        supabase_request(
            "POST",
            "/rest/v1/voice_note_jobs",
            json=record,
            headers={"Prefer": "return=minimal"},
        )
        dispatch(job_id)
    except Exception:
        try:
            supabase_request("DELETE", f"/rest/v1/voice_note_jobs?id=eq.{job_id}")
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="job_submission_failed") from None
    return {"job_id": job_id, "status": "queued"}


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(bot_auth)])
def get_job_status(job_id: str):
    purge_expired()
    job = bot_job(job_id)
    return {
        "job_id": job["id"],
        "status": job["status"],
        "duration_seconds": job.get("duration_seconds"),
        "expires_at": job.get("expires_at"),
    }


@app.get("/v1/jobs/{job_id}/result", dependencies=[Depends(bot_auth)])
def get_result(job_id: str, background: BackgroundTasks):
    job = bot_job(job_id)
    if job.get("status") != "completed" or not job.get("result_path"):
        raise HTTPException(status_code=409, detail="result_not_ready")
    try:
        audio = storage_download(job["result_path"])
    except Exception:
        raise HTTPException(status_code=502, detail="result_unavailable") from None
    background.add_task(delete_job, job)
    bundled = str(job["result_path"]).lower().endswith(".zip")
    return Response(
        content=audio,
        media_type="application/zip" if bundled else "audio/ogg",
        headers={
            "Content-Disposition": f'attachment; filename="voice-notes-{job_id[:8]}.zip"' if bundled else f'attachment; filename="voice-note-{job_id[:8]}.ogg"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/v1/jobs/{job_id}/claim")
def claim_job(job_id: str, claims: dict = Depends(worker_claims)):
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="job_not_found")
    run_id = str(claims["run_id"])
    response = supabase_request("POST", "/rest/v1/rpc/claim_voice_note_job", json={"p_job_id": job_id, "p_run_id": run_id})
    rows = response.json()
    if not rows:
        raise HTTPException(status_code=409, detail="job_not_claimable")
    job = rows[0] if isinstance(rows, list) else rows
    try:
        voice_samples = job.get("voice_samples")
        if not isinstance(voice_samples, list) or not voice_samples:
            voice_samples = [{"sample_path": job["sample_path"], "reference_text": job["reference_text"]}]
        signed_samples = []
        for item in voice_samples:
            sample_url = signed_sample_url(str(item["sample_path"]))
            signed_samples.append({
                "reference_text": str(item["reference_text"]),
                "voice_sample_url": sample_url,
                "voice_sample_host": urlparse(sample_url).hostname,
                "voice_sample_filename": str(item["sample_path"]).rsplit("/", 1)[-1],
            })
    except Exception:
        raise HTTPException(status_code=502, detail="sample_unavailable") from None
    return {
        "text": job["text"],
        "language": job["language"],
        "reference_text": job["reference_text"],
        "voice_samples": signed_samples,
        "reference_text": signed_samples[0]["reference_text"],
        "voice_sample_url": signed_samples[0]["voice_sample_url"],
        "voice_sample_host": signed_samples[0]["voice_sample_host"],
        "voice_sample_filename": signed_samples[0]["voice_sample_filename"],
    }


@app.post("/v1/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    audio: Annotated[UploadFile, File()],
    duration_seconds: Annotated[float, Form()],
    claims: dict = Depends(worker_claims),
):
    job = require_job_run(job_id, claims)
    if audio.content_type not in {"audio/ogg", "application/zip"} or not 0 < duration_seconds <= 1200:
        raise HTTPException(status_code=400, detail="invalid_audio")
    content = await audio.read(MAX_OUTPUT_BYTES + 1)
    if not content or len(content) > MAX_OUTPUT_BYTES:
        raise HTTPException(status_code=413, detail="audio_too_large")
    bundled = audio.content_type == "application/zip"
    if bundled and not zipfile.is_zipfile(io.BytesIO(content)):
        raise HTTPException(status_code=400, detail="invalid_audio")
    result_path = f"results/{job_id}.zip" if bundled else f"results/{job_id}.ogg"
    try:
        storage_upload(result_path, content, "application/zip" if bundled else "audio/ogg")
        updated = supabase_request(
            "PATCH",
            f"/rest/v1/voice_note_jobs?id=eq.{job_id}&status=eq.processing&run_id=eq.{claims['run_id']}",
            json={"status": "completed", "result_path": result_path, "duration_seconds": duration_seconds},
            headers={"Prefer": "return=representation"},
        ).json()
        if not updated:
            storage_delete([result_path])
            raise HTTPException(status_code=409, detail="job_update_conflict")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="result_store_failed") from None
    return {"status": "completed"}


@app.post("/v1/jobs/{job_id}/fail")
def fail_job(job_id: str, body: dict, claims: dict = Depends(worker_claims)):
    require_job_run(job_id, claims)
    code = body.get("code")
    allowed = {
        "invalid_job", "generation_timeout", "voicebox_start_timeout", "audio_too_large",
        "bridge_not_configured", "oidc_unavailable", "sample_unavailable", "voicebox_source_missing",
        "voicebox_start_failed", "profile_create_failed", "profile_sample_failed", "generation_start_failed",
        "generation_status_failed", "generation_failed", "audio_fetch_failed", "audio_conversion_failed",
        "audio_validation_failed", "bridge_complete_failed", "worker_error",
    }
    if code not in allowed:
        code = "worker_error"
    supabase_request(
        "PATCH",
        f"/rest/v1/voice_note_jobs?id=eq.{job_id}&status=eq.processing&run_id=eq.{claims['run_id']}",
        json={"status": "failed", "error_code": code},
        headers={"Prefer": "return=minimal"},
    )
    return {"status": "failed", "code": code}
