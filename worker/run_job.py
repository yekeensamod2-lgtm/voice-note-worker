from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
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


def validate_job(job: dict) -> tuple[str, str, str, str, str]:
    text = job.get("text")
    language = job.get("language")
    reference_text = job.get("reference_text")
    sample_url = job.get("voice_sample_url")
    sample_filename = job.get("voice_sample_filename")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        invalid_job("text")
    if not isinstance(language, str) or not re.fullmatch(r"(en|zh|ja|ko|de|fr|ru|pt|es|it)", language):
        invalid_job("language")
    if not isinstance(reference_text, str) or not reference_text.strip() or len(reference_text) > 1000:
        invalid_job("reference_text")
    if not isinstance(sample_url, str):
        invalid_job("sample_url_missing")
    parsed = urlparse(sample_url)
    allowed_hosts = {
        host.strip().lower()
        for host in os.environ.get("VOICE_SAMPLE_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed_hosts:
        invalid_job("sample_url_host_or_scheme")
    if parsed.username or parsed.password:
        invalid_job("sample_url_credentials")
    if not isinstance(sample_filename, str) or Path(sample_filename).suffix.lower() not in {
        ".wav", ".mp3", ".m4a", ".ogg", ".opus", ".webm", ".flac", ".aac"
    }:
        invalid_job("sample_filename")
    return text, language, reference_text, sample_url, sample_filename


def download_sample(url: str, path: Path) -> None:
    response = requests.get(url, stream=True, timeout=(15, 90), allow_redirects=False)
    if response.status_code != 200:
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


def create_profile(sample_path: Path, language: str, reference_text: str) -> str:
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


def generate(profile_id: str, text: str, language: str, output_dir: Path) -> Path:
    response = requests.post(
        f"{VOICEBOX_URL}/generate",
        json={
            "profile_id": profile_id,
            "text": text,
            "language": language,
            "engine": "qwen",
            "model_size": "0.6B",
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


def send_complete(token: str, ogg_path: Path, duration: float) -> None:
    with ogg_path.open("rb") as audio:
        response = bridge_call(
            "POST",
            f"/v1/jobs/{JOB_ID}/complete",
            token,
            files={"audio": ("voice-note.ogg", audio, "audio/ogg")},
            data={"duration_seconds": f"{duration:.2f}"},
        )
    if response.status_code not in (200, 204):
        raise RuntimeError("bridge_complete_failed")


def send_failure(token: str, code: str) -> None:
    if not token or not BRIDGE or not JOB_ID:
        return
    try:
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
        text, language, reference_text, sample_url, sample_filename = validate_job(job)
        with tempfile.TemporaryDirectory(prefix="voicebox-job-") as temp:
            temp_dir = Path(temp)
            sample_path = temp_dir / sample_filename
            download_sample(sample_url, sample_path)
            process = start_voicebox()
            wait_for_voicebox(process)
            profile_id = create_profile(sample_path, language, reference_text)
            wav_path = generate(profile_id, text, language, temp_dir)
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
