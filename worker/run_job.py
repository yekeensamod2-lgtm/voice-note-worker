from __future__ import annotations

import json
import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("voice_worker")
BRIDGE = os.environ.get("VOICEBOX_BRIDGE_URL", "").rstrip("/")
JOB_ID = os.environ.get("VOICE_JOB_ID", "")
VOICEBOX_ROOT = Path("voicebox-src").resolve()
VOICEBOX_DATA = Path(os.environ.get("VOICEBOX_DATA_DIR", "voicebox-data")).resolve()
VOICEBOX_URL = "http://127.0.0.1:17493"
MAX_TEXT_CHARS = 12_000
MAX_SAMPLE_BYTES = 25 * 1024 * 1024
MAX_AUDIO_BYTES = 120 * 1024 * 1024
MAX_VOICE_CLIPS = 20
MAX_CLIP_SECONDS = 45.0
CLIP_PLAN_PREFIX = "ASTRA_VOICE_CLIP_PLAN_V1\n"
CLIP_PLAN_FORMAT = "astra-voice-clip-plan-v1"


def safe_log(message: str) -> None:
    LOG.info("job=%s %s", JOB_ID[:8], message)


def invalid_job(reason: str) -> None:
    # Keep payload values private; this reason identifies only the rejected field.
    safe_log(f"job validation failed ({reason})")
    raise ValueError("invalid_job")


def oidc_token() -> str:
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not request_token:
        raise RuntimeError("oidc_unavailable")
    separator = "&" if "?" in url else "?"
    response = requests.get(
        f"{url}{separator}audience=voicebox-bridge",
        headers={"Authorization": f"bearer {request_token}"},
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError("oidc_unavailable")
    token = response.json().get("value")
    if not isinstance(token, str) or not token:
        raise RuntimeError("oidc_unavailable")
    return token


def bridge_call(method: str, path: str, token: str, **kwargs) -> requests.Response:
    if not BRIDGE.startswith("https://"):
        raise RuntimeError("bridge_not_configured")
    response = requests.request(
        method,
        f"{BRIDGE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=(15, 120),
        **kwargs,
    )
    return response


def validate_job(job: dict) -> tuple[str, str, list[tuple[str, str, str]]]:
    text = job.get("text")
    language = job.get("language")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        invalid_job("text")
    if not isinstance(language, str) or not re.fullmatch(r"(en|zh|ja|ko|de|fr|ru|pt|es|it)", language):
        invalid_job("language")
    samples = job.get("voice_samples")
    if not isinstance(samples, list) or not samples:
        # Compatibility with bridge payloads created before multi-sample support.
        samples = [{
            "reference_text": job.get("reference_text"),
            "voice_sample_url": job.get("voice_sample_url"),
            "voice_sample_host": job.get("voice_sample_host"),
            "voice_sample_filename": job.get("voice_sample_filename"),
        }]
    if not 1 <= len(samples) <= 5:
        invalid_job("sample_count")
    validated: list[tuple[str, str, str]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            invalid_job("sample_record")
        reference_text = sample.get("reference_text")
        sample_url = sample.get("voice_sample_url")
        sample_host = sample.get("voice_sample_host")
        sample_filename = sample.get("voice_sample_filename")
        if not isinstance(reference_text, str) or not reference_text.strip() or len(reference_text) > 1000:
            invalid_job("reference_text")
        if not isinstance(sample_url, str):
            invalid_job("sample_url_missing")
        parsed = urlparse(sample_url)
        if parsed.scheme != "https" or not parsed.hostname or not isinstance(sample_host, str) or not sample_host.strip():
            invalid_job("sample_url_scheme_or_host")
        if parsed.hostname.lower() != sample_host.strip().lower():
            invalid_job("sample_url_host_mismatch")
        if parsed.username or parsed.password:
            invalid_job("sample_url_credentials")
        if not isinstance(sample_filename, str) or Path(sample_filename).suffix.lower() not in {
            ".wav", ".mp3", ".m4a", ".ogg", ".opus", ".webm", ".flac", ".aac"
        }:
            invalid_job("sample_filename")
        validated.append((sample_url, sample_filename, reference_text))
    return text, language, validated


def _normalize_spoken_text(text: str) -> str:
    return " ".join(re.sub(r"\{br\}", " ", str(text or ""), flags=re.I).split())


def decode_clip_plan(text: str) -> tuple[dict, bool]:
    """Validate an Astra Gemini plan, or safely treat a legacy request as one clip."""
    if not text.startswith(CLIP_PLAN_PREFIX):
        source = _normalize_spoken_text(text)
        if not source:
            invalid_job("text")
        return {
            "format": CLIP_PLAN_FORMAT,
            "source_text": source,
            "clips": [{"sentences": [{"text": source, "estimated_seconds": max(1.0, len(source.split()) / 2.3)}]}],
        }, False
    try:
        plan = json.loads(text[len(CLIP_PLAN_PREFIX):])
    except (json.JSONDecodeError, TypeError):
        invalid_job("clip_plan_json")
    if not isinstance(plan, dict) or plan.get("format") != CLIP_PLAN_FORMAT:
        invalid_job("clip_plan_format")
    source = _normalize_spoken_text(plan.get("source_text", ""))
    clips = plan.get("clips")
    if not source or not isinstance(clips, list) or not 1 <= len(clips) <= MAX_VOICE_CLIPS:
        invalid_job("clip_plan_shape")
    sentences = []
    clean_clips = []
    for clip in clips:
        units = clip.get("sentences") if isinstance(clip, dict) else None
        if not isinstance(units, list) or not units:
            invalid_job("clip_plan_sentences")
        clean_units = []
        for unit in units:
            if not isinstance(unit, dict):
                invalid_job("clip_plan_sentence")
            sentence = _normalize_spoken_text(unit.get("text", ""))
            estimate = unit.get("estimated_seconds")
            if not sentence or not isinstance(estimate, (int, float)) or isinstance(estimate, bool) or not 0 < float(estimate) <= 600:
                invalid_job("clip_plan_sentence")
            clean_units.append({"text": sentence, "estimated_seconds": float(estimate)})
            sentences.append(sentence)
        clean_clips.append({"sentences": clean_units})
    if _normalize_spoken_text(" ".join(sentences)) != source:
        invalid_job("clip_plan_changed_text")
    for sentence in sentences[:-1]:
        if not re.search(r"[.!?…](?:[\"'’”\)\]}]*)(?:[\U0001F000-\U0001FAFF\uFE0F\u200D])*\s*$", sentence):
            invalid_job("clip_plan_split_sentence")
    return {"format": CLIP_PLAN_FORMAT, "source_text": source, "clips": clean_clips}, True


def download_sample(url: str, path: Path) -> None:
    response = requests.get(url, stream=True, timeout=(15, 90), allow_redirects=False)
    if response.status_code != 200:
        safe_log(f"sample download returned HTTP {response.status_code}")
        raise RuntimeError("sample_unavailable")
    length = response.headers.get("Content-Length")
    if length and int(length) > MAX_SAMPLE_BYTES:
        invalid_job("sample_size")
    total = 0
    with path.open("wb") as output:
        for chunk in response.iter_content(1024 * 256):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_SAMPLE_BYTES:
                invalid_job("sample_size")
            output.write(chunk)
    if total == 0:
        raise RuntimeError("sample_unavailable")


def start_voicebox() -> subprocess.Popen:
    if not (VOICEBOX_ROOT / "backend" / "main.py").is_file():
        raise RuntimeError("voicebox_source_missing")
    VOICEBOX_DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["VOICEBOX_MODELS_DIR"] = str(Path(os.environ.get("VOICEBOX_MODELS_DIR", "" )).resolve())
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    return subprocess.Popen(
        ["python", "-m", "backend.main", "--host", "127.0.0.1", "--port", "17493", "--data-dir", str(VOICEBOX_DATA)],
        cwd=VOICEBOX_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_voicebox(process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("voicebox_start_failed")
        try:
            response = requests.get(f"{VOICEBOX_URL}/health", timeout=3)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError("voicebox_start_timeout")


def create_profile(samples: list[tuple[Path, str]], language: str) -> str:
    profile = requests.post(
        f"{VOICEBOX_URL}/profiles",
        json={"name": "Temporary job voice", "language": language, "voice_type": "cloned", "default_engine": "qwen"},
        timeout=30,
    )
    if profile.status_code != 200:
        raise RuntimeError("profile_create_failed")
    profile_id = profile.json().get("id")
    if not profile_id:
        raise RuntimeError("profile_create_failed")
    for sample_path, reference_text in samples:
        with sample_path.open("rb") as audio:
            added = requests.post(
                f"{VOICEBOX_URL}/profiles/{profile_id}/samples",
                files={"file": (sample_path.name, audio, "application/octet-stream")},
                data={"reference_text": reference_text},
                timeout=(20, 180),
            )
        if added.status_code not in (200, 201):
            raise RuntimeError("profile_sample_failed")
    return profile_id


def stable_voice_seed(sample_paths: list[Path]) -> int:
    """Keep sampling consistent for this exact set of private reference recordings."""
    digest = hashlib.sha256()
    for sample_path in sample_paths:
        digest.update(sample_path.name.encode("utf-8"))
        with sample_path.open("rb") as sample:
            for chunk in iter(lambda: sample.read(1024 * 1024), b""):
                digest.update(chunk)
    # Voicebox accepts a non-negative integer seed. Keep it bounded to 31 bits.
    return int.from_bytes(digest.digest()[:4], "big") & 0x7FFFFFFF


def generate(profile_id: str, text: str, language: str, output_dir: Path, seed: int) -> Path:
    response = requests.post(
        f"{VOICEBOX_URL}/generate",
        json={
            "profile_id": profile_id,
            "text": text,
            "language": language,
            "engine": "qwen",
            "model_size": "1.7B",
            "seed": seed,
            "normalize": True,
            "max_chunk_chars": 800,
        },
        timeout=(20, 120),
    )
    if response.status_code not in (200, 201):
        raise RuntimeError("generation_start_failed")
    generation_id = response.json().get("id")
    if not generation_id:
        raise RuntimeError("generation_start_failed")

    deadline = time.monotonic() + 80 * 60
    status_url = f"{VOICEBOX_URL}/generate/{generation_id}/status"
    while time.monotonic() < deadline:
        with requests.get(status_url, stream=True, timeout=(10, 90)) as stream:
            if stream.status_code != 200:
                raise RuntimeError("generation_status_failed")
            for line in stream.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if not line or not line.startswith("data:"):
                    continue
                try:
                    state = json.loads(line[5:].strip())
                except (json.JSONDecodeError, TypeError):
                    continue
                if state.get("status") == "failed":
                    raise RuntimeError("generation_failed")
                if state.get("status") == "completed":
                    audio_response = requests.get(
                        f"{VOICEBOX_URL}/audio/{generation_id}",
                        stream=True,
                        timeout=(15, 120),
                    )
                    if audio_response.status_code != 200:
                        raise RuntimeError("audio_fetch_failed")
                    wav_path = output_dir / "generated.wav"
                    size = 0
                    with wav_path.open("wb") as wav:
                        for chunk in audio_response.iter_content(1024 * 256):
                            if chunk:
                                size += len(chunk)
                                if size > MAX_AUDIO_BYTES:
                                    raise RuntimeError("audio_too_large")
                                wav.write(chunk)
                    if size == 0:
                        raise RuntimeError("audio_fetch_failed")
                    return wav_path
        time.sleep(2)
    raise TimeoutError("generation_timeout")


def convert_to_ogg_opus(wav_path: Path, ogg_path: Path) -> float:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav_path), "-vn", "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "48k", "-application", "voip", str(ogg_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=300,
    )
    if result.returncode != 0 or not ogg_path.is_file() or ogg_path.stat().st_size == 0:
        raise RuntimeError("audio_conversion_failed")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(ogg_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
        text=True,
    )
    if probe.returncode != 0:
        raise RuntimeError("audio_validation_failed")
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    if duration <= 0 or duration > 600:
        raise RuntimeError("audio_validation_failed")
    return duration


def split_sentence_units(units: list[dict]) -> list[list[dict]]:
    """Choose the sentence boundary closest to half the estimate for a long clip."""
    if len(units) < 2:
        return [units]
    total = sum(float(unit["estimated_seconds"]) for unit in units)
    running = 0.0
    candidates = []
    for index in range(1, len(units)):
        running += float(units[index - 1]["estimated_seconds"])
        candidates.append((abs(total / 2 - running), index))
    _, split_at = min(candidates)
    return [units[:split_at], units[split_at:]]


def generate_clip_bundle(profile_id: str, plan: dict, language: str, output_dir: Path, seed: int) -> tuple[Path, float, int]:
    pending = list(plan["clips"])
    generated = []
    while pending:
        clip = pending.pop(0)
        units = clip["sentences"]
        text = " ".join(unit["text"] for unit in units)
        attempt_dir = output_dir / f"attempt-{uuid.uuid4().hex}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        wav_path = generate(profile_id, text, language, attempt_dir, seed)
        ogg_path = attempt_dir / "voice-note.ogg"
        duration = convert_to_ogg_opus(wav_path, ogg_path)
        current_total = len(generated) + len(pending)
        if duration > MAX_CLIP_SECONDS and len(units) > 1 and current_total + 2 <= MAX_VOICE_CLIPS:
            left, right = split_sentence_units(units)
            # Re-generate only as sentence-aligned clips; never split a sentence
            # to force an audio-duration limit.
            shutil.rmtree(attempt_dir, ignore_errors=True)
            pending = [{"sentences": left}, {"sentences": right}] + pending
            continue
        generated.append({"directory": attempt_dir, "path": ogg_path, "text": text, "duration_seconds": duration})
        if len(generated) > MAX_VOICE_CLIPS:
            raise ValueError("too_many_voice_clips")

    total_duration = sum(item["duration_seconds"] for item in generated)
    if total_duration > 1200:
        raise RuntimeError("audio_validation_failed")
    manifest = {
        "version": 1,
        "source_text": plan["source_text"],
        "clips": [{"file": f"clip-{index:02d}.ogg", "text": item["text"], "duration_seconds": round(item["duration_seconds"], 2)} for index, item in enumerate(generated, 1)],
    }
    archive_path = output_dir / "voice-notes.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, item in enumerate(generated, 1):
            archive.write(item["path"], arcname=f"clip-{index:02d}.ogg")
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    if archive_path.stat().st_size > MAX_AUDIO_BYTES:
        raise RuntimeError("audio_too_large")
    return archive_path, total_duration, len(generated)


def send_complete(token: str, audio_path: Path, duration: float, content_type: str = "audio/ogg") -> None:
    # GitHub OIDC tokens are short-lived. Model setup and synthesis can take
    # several minutes, so use a fresh token for the result upload.
    token = oidc_token()
    extension = ".zip" if content_type == "application/zip" else ".ogg"
    with audio_path.open("rb") as audio:
        response = bridge_call(
            "POST",
            f"/v1/jobs/{JOB_ID}/complete",
            token,
            files={"audio": (f"voice-notes{extension}", audio, content_type)},
            data={"duration_seconds": f"{duration:.2f}"},
        )
    if response.status_code not in (200, 204):
        # Keep response bodies private; the status is enough to troubleshoot
        # bridge-side rejection without risking message or infrastructure data.
        safe_log(f"bridge completion rejected (HTTP {response.status_code})")
        raise RuntimeError("bridge_complete_failed")


def send_failure(token: str, code: str) -> None:
    if not token or not BRIDGE or not JOB_ID:
        return
    try:
        # The token used to claim the job may have expired while processing.
        token = oidc_token()
        bridge_call(
            "POST",
            f"/v1/jobs/{JOB_ID}/fail",
            token,
            json={"code": code},
        )
    except requests.RequestException:
        pass


def main() -> int:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", JOB_ID):
        safe_log("rejected invalid opaque job id")
        return 2
    if not BRIDGE.startswith("https://"):
        safe_log("bridge is not configured")
        return 2
    token = ""
    process = None
    try:
        token = oidc_token()
        claim = bridge_call("POST", f"/v1/jobs/{JOB_ID}/claim", token)
        if claim.status_code == 409:
            safe_log("job was already claimed; no duplicate work started")
            return 0
        if claim.status_code != 200:
            raise RuntimeError("job_claim_failed")
        job = claim.json()
        text, language, sample_records = validate_job(job)
        clip_plan, planned = decode_clip_plan(text)
        with tempfile.TemporaryDirectory(prefix="voicebox-job-") as temp:
            temp_dir = Path(temp)
            sample_paths: list[Path] = []
            profile_samples: list[tuple[Path, str]] = []
            for index, (sample_url, sample_filename, reference_text) in enumerate(sample_records, 1):
                sample_path = temp_dir / f"sample-{index:02d}{Path(sample_filename).suffix.lower()}"
                download_sample(sample_url, sample_path)
                sample_paths.append(sample_path)
                profile_samples.append((sample_path, reference_text))
            process = start_voicebox()
            wait_for_voicebox(process)
            profile_id = create_profile(profile_samples, language)
            seed = stable_voice_seed(sample_paths)
            if planned:
                bundle_path, duration, clip_count = generate_clip_bundle(profile_id, clip_plan, language, temp_dir, seed)
                send_complete(token, bundle_path, duration, "application/zip")
                safe_log(f"completed {clip_count} voice clip(s); total_seconds={duration:.1f}")
            else:
                wav_path = generate(profile_id, clip_plan["source_text"], language, temp_dir, seed)
                ogg_path = temp_dir / "voice-note.ogg"
                duration = convert_to_ogg_opus(wav_path, ogg_path)
                send_complete(token, ogg_path, duration)
        safe_log("completed audio generation and private upload")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, (ValueError, TimeoutError)) and str(exc) in {
            "invalid_job", "generation_timeout", "voicebox_start_timeout", "audio_too_large"
        } else "worker_error"
        if isinstance(exc, RuntimeError) and str(exc) in {
            "bridge_not_configured", "oidc_unavailable", "sample_unavailable", "voicebox_source_missing",
            "voicebox_start_failed", "profile_create_failed", "profile_sample_failed", "generation_start_failed",
            "generation_status_failed", "generation_failed", "audio_fetch_failed", "audio_conversion_failed",
            "audio_validation_failed", "bridge_complete_failed",
        }:
            code = str(exc)
        safe_log(f"failed safely ({code})")
        send_failure(token, code)
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(VOICEBOX_DATA, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
