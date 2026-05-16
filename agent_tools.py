"""
agent_tools.py

Tools for an automated high-end web design workflow:
1) Generate Kling/PiAPI video assets from prompts.
2) Extract scroll-animation frames from MP4 videos via FFmpeg.
3) Push generated website changes to GitHub, triggering Vercel.

IMPORTANT:
- Install dependencies first: requests + ffmpeg + git.
- Replace the visible placeholders below OR set environment variables.
- Do not commit real API keys to GitHub.
"""

from __future__ import annotations

# ============================================================================
# 🔐 API KEY PLACEHOLDERS — HIER DEINE KEYS EINTRAGEN ODER ENV VARS SETZEN
# ============================================================================
# Empfohlen: PIAPI_API_KEY als Umgebungsvariable setzen.
# Windows PowerShell: setx PIAPI_API_KEY "dein_key"
# macOS/Linux:        export PIAPI_API_KEY="dein_key"
# Direkt im Skript geht auch, aber echte Keys niemals committen.
PIAPI_API_KEY = "PASTE_YOUR_PIAPI_API_KEY_HERE"
PIAPI_BASE_URL = "https://api.piapi.ai"
# ============================================================================

import glob
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
ASSET_DIR = PROJECT_ROOT / "assets" / "videos"
FRAME_DIR = PROJECT_ROOT / "assets" / "frames"


class ToolError(RuntimeError):
    """Raised when a workflow tool fails with a human-readable message."""


def _get_piapi_key() -> str:
    key = os.getenv("PIAPI_API_KEY") or PIAPI_API_KEY
    if not key or key == "PASTE_YOUR_PIAPI_API_KEY_HERE":
        raise ToolError(
            "PIAPI_API_KEY fehlt. Setze die Umgebungsvariable PIAPI_API_KEY "
            "oder trage den Key oben in agent_tools.py ein."
        )
    return key


def _piapi_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_piapi_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name.strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        raise ValueError("output_name darf nicht leer sein")
    return cleaned


def _resolve_executable(executable: str) -> str:
    found = shutil.which(executable)
    if found:
        return found
    if executable.lower() == "ffmpeg":
        winget_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        matches = glob.glob(str(winget_root / "**" / "ffmpeg.exe"), recursive=True)
        if matches:
            return matches[0]
    return executable


def _run_command(command: list[str], cwd: Path | None = None) -> str:
    command = [_resolve_executable(command[0]), *command[1:]]
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd or PROJECT_ROOT),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.stdout.strip() or completed.stderr.strip()
    except FileNotFoundError as exc:
        raise ToolError(f"Befehl nicht gefunden: {command[0]}. Ist er installiert und im PATH?") from exc
    except subprocess.CalledProcessError as exc:
        cmd = " ".join(shlex.quote(part) for part in command)
        details = (exc.stderr or exc.stdout or "").strip()
        raise ToolError(f"Befehl fehlgeschlagen: {cmd}\n{details}") from exc


def _extract_video_url(task_data: dict[str, Any]) -> str | None:
    """Check common PiAPI response locations for the finished video URL."""
    candidates: list[Any] = [
        task_data.get("video_url"),
        task_data.get("url"),
        task_data.get("output_url"),
    ]
    for container_key in ("result", "output", "data"):
        container = task_data.get(container_key)
        if isinstance(container, dict):
            candidates.extend([container.get("video_url"), container.get("url"), container.get("output_url")])
    for list_key in ("videos", "assets", "files"):
        value = task_data.get(list_key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    candidates.extend([item.get("url"), item.get("video_url")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    return None


def generate_kling_asset(
    prompt: str,
    output_name: str,
    *,
    model: str = "kling-3.0",
    duration: int = 5,
    aspect_ratio: str = "16:9",
    poll_interval_seconds: int = 8,
    timeout_seconds: int = 900,
) -> str:
    """
    Generate a Kling 3.0 video via PiAPI, poll until finished, download it as MP4.

    Returns the absolute MP4 path.

    NOTE: PiAPI endpoint/payload naming can differ by account/model release. This
    uses the common task endpoint pattern. If your PiAPI dashboard shows another
    exact endpoint for Kling 3.0, adjust create_url/status_url/payload here only.
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt darf nicht leer sein")

    safe_output = _safe_name(output_name)
    if not safe_output.lower().endswith(".mp4"):
        safe_output += ".mp4"

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    output_path = ASSET_DIR / safe_output

    create_url = f"{PIAPI_BASE_URL.rstrip('/')}/api/v1/task"
    payload = {
        "model": model,
        "task_type": "video_generation",
        "input": {
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
        },
    }

    create_resp = requests.post(create_url, headers=_piapi_headers(), json=payload, timeout=60)
    if create_resp.status_code >= 400:
        raise ToolError(f"PiAPI create task failed ({create_resp.status_code}): {create_resp.text}")

    create_data = create_resp.json()
    task_id = (
        create_data.get("task_id")
        or create_data.get("id")
        or create_data.get("data", {}).get("task_id")
        or create_data.get("data", {}).get("id")
    )
    if not task_id:
        raise ToolError(f"Keine task_id in PiAPI-Antwort gefunden: {json.dumps(create_data, indent=2)}")

    deadline = time.time() + timeout_seconds
    last_status = "unknown"
    while time.time() < deadline:
        status_url = f"{PIAPI_BASE_URL.rstrip('/')}/api/v1/task/{task_id}"
        status_resp = requests.get(status_url, headers=_piapi_headers(), timeout=60)
        if status_resp.status_code >= 400:
            raise ToolError(f"PiAPI status polling failed ({status_resp.status_code}): {status_resp.text}")

        status_json = status_resp.json()
        task_data = status_json.get("data", status_json) if isinstance(status_json, dict) else {}
        status = str(task_data.get("status") or task_data.get("state") or "").lower()
        last_status = status or last_status

        if status in {"completed", "complete", "succeeded", "success", "finished"}:
            video_url = _extract_video_url(task_data)
            if not video_url:
                raise ToolError(f"Task fertig, aber keine Video-URL gefunden: {json.dumps(task_data, indent=2)}")
            download_resp = requests.get(video_url, stream=True, timeout=120)
            if download_resp.status_code >= 400:
                raise ToolError(f"Video-Download fehlgeschlagen ({download_resp.status_code}): {download_resp.text[:500]}")
            with output_path.open("wb") as file:
                for chunk in download_resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)
            return str(output_path.resolve())

        if status in {"failed", "failure", "error", "cancelled", "canceled"}:
            raise ToolError(f"PiAPI task failed: {json.dumps(task_data, indent=2)}")

        time.sleep(poll_interval_seconds)

    raise TimeoutError(f"PiAPI task {task_id} nicht innerhalb von {timeout_seconds}s fertig. Letzter Status: {last_status}")


def extract_frames_ffmpeg(
    video_path: str,
    output_folder: str,
    *,
    fps: int = 24,
    quality: int = 3,
    max_width: int = 1920,
) -> str:
    """
    Extract high-quality compressed JPEG frames from a local MP4 via FFmpeg.

    Returns the absolute frame folder path. Output files: frame_000001.jpg etc.
    Lower quality value means better/larger JPEGs. Recommended: q:v 2-5.
    """
    source = Path(video_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Video nicht gefunden: {source}")

    destination = Path(output_folder).expanduser()
    if not destination.is_absolute():
        destination = FRAME_DIR / destination
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    frame_pattern = destination / "frame_%06d.jpg"
    vf = f"fps={fps},scale='min({max_width},iw)':-2:flags=lanczos"
    _run_command([
        "ffmpeg", "-y", "-i", str(source),
        "-vf", vf,
        "-q:v", str(quality),
        "-pix_fmt", "yuvj420p",
        str(frame_pattern),
    ])
    return str(destination)


def push_to_github(commit_message: str) -> str:
    """
    Run git add ., git commit -m commit_message, and git push.
    A connected Vercel project will deploy automatically after push.
    """
    if not commit_message or not commit_message.strip():
        raise ValueError("commit_message darf nicht leer sein")
    if not (PROJECT_ROOT / ".git").exists():
        raise ToolError(f"Kein Git-Repository gefunden: {PROJECT_ROOT}. Führe zuerst git init + remote add aus.")

    outputs: list[str] = []
    outputs.append(_run_command(["git", "add", "."], cwd=PROJECT_ROOT))

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(PROJECT_ROOT),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()

    if status:
        outputs.append(_run_command(["git", "commit", "-m", commit_message], cwd=PROJECT_ROOT))
    else:
        outputs.append("Keine Änderungen zum Committen.")

    outputs.append(_run_command(["git", "push"], cwd=PROJECT_ROOT))
    return "\n".join(part for part in outputs if part)


if __name__ == "__main__":
    print("agent_tools.py geladen. Importiere die Funktionen in deinem Agent-/Function-Calling-Setup.")
