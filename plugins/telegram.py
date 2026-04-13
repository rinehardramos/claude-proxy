"""Telegram notification plugin for claude-proxy.

Sends Claude's full response to a Telegram chat, splitting into chunks
if needed or converting to a voice message via TTS for very long responses.
Stdlib only (+ subprocess for TTS).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

_bot_token: str | None = None
_chat_id: str | None = None
_project_name: str = ""
_audio_threshold: int = 8192
_tts_engine: str = "say"
_tts_openai_model: str = "tts-1"
_tts_openai_voice: str = "alloy"
_tts_openai_api_key: str | None = None
_voice_upload_timeout: int = 300

MAX_TG_LENGTH = 4096
MAX_TG_CAPTION = 1024


# ── TTS Status Tracking ──────────────────────────────────────────────────

_tts_status: dict = {}


def tts_status() -> dict | None:
    """Return current TTS task status or None if idle."""
    return _tts_status.copy() if _tts_status else None


def _update_status(uid: str, stage: str, **kwargs) -> None:
    """Thread-safe status update (GIL protects single dict assignment)."""
    global _tts_status
    elapsed = time.monotonic() - _tts_status.get("start_mono", time.monotonic())
    _tts_status = {
        "uid": uid,
        "stage": stage,
        "elapsed": round(elapsed, 1),
        **kwargs,
    }


def _clear_status() -> None:
    global _tts_status
    _tts_status = {}


# ── Dynamic Timeout ──────────────────────────────────────────────────────

def _estimate_timeout(text_len: int, base: int = 30, per_1k_chars: int = 15) -> int:
    """Estimate subprocess timeout from text length. Min 60s."""
    return max(60, base + per_1k_chars * (text_len // 1000))


# ── TTS Engine Registry ──────────────────────────────────────────────────

_TTS_REGISTRY: list[dict] = []


def _register_tts(name: str, check_fn, generate_fn) -> None:
    """Register a TTS engine.

    Args:
        name: Short identifier (e.g. "say", "openai").
        check_fn: () -> str|None  — returns None if ready, or error string.
        generate_fn: (text, tmp_dir, uid) -> str|None  — returns OGG path or None.
    """
    _TTS_REGISTRY.append({"name": name, "check": check_fn, "generate": generate_fn})


# ── Built-in TTS engines ─────────────────────────────────────────────────

def _check_say() -> str | None:
    if not shutil.which("say"):
        return "say command not found (macOS only)"
    if not shutil.which("ffmpeg"):
        return "ffmpeg not installed (brew install ffmpeg)"
    return None


def _generate_say(text: str, tmp_dir: str, uid: str) -> str | None:
    aiff_path = os.path.join(tmp_dir, f"tg_{uid}.aiff")
    ogg_path = os.path.join(tmp_dir, f"tg_{uid}.ogg")
    timeout = _estimate_timeout(len(text))
    try:
        _update_status(uid, "encoding", engine="say", est_timeout=timeout)
        _log(f"TTS [{uid}] encoding with say (est. timeout: {timeout}s)")
        t0 = time.monotonic()
        subprocess.run(
            ["say", "-o", aiff_path, text],
            check=True, timeout=timeout, capture_output=True,
        )
        enc_elapsed = time.monotonic() - t0
        _update_status(uid, "converting", engine="say")
        _log(f"TTS [{uid}] encoding complete ({enc_elapsed:.1f}s), converting to OGG...")
        subprocess.run(
            ["ffmpeg", "-y", "-i", aiff_path, "-c:a", "libopus", "-b:a", "64000", ogg_path],
            check=True, timeout=120, capture_output=True,
        )
        _cleanup(aiff_path)
        return ogg_path
    except Exception as exc:
        _log(f"TTS [{uid}] say failed: {exc}")
        _cleanup(aiff_path)
        _cleanup(ogg_path)
        return None


def _check_openai() -> str | None:
    if not _tts_openai_api_key:
        return "OPENAI_API_KEY not set"
    return None


def _generate_openai(text: str, tmp_dir: str, uid: str) -> str | None:
    """OpenAI TTS API → OGG Opus (no ffmpeg needed)."""
    ogg_path = os.path.join(tmp_dir, f"tg_{uid}.ogg")
    timeout = _estimate_timeout(len(text))
    try:
        _update_status(uid, "encoding", engine="openai", est_timeout=timeout)
        _log(f"TTS [{uid}] encoding with openai (est. timeout: {timeout}s)")
        body = json.dumps({
            "model": _tts_openai_model,
            "input": text,
            "voice": _tts_openai_voice,
            "response_format": "opus",
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_tts_openai_api_key}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        with open(ogg_path, "wb") as f:
            f.write(resp.read())
        return ogg_path
    except Exception as exc:
        _log(f"TTS [{uid}] openai failed: {exc}")
        _cleanup(ogg_path)
        return None


def _check_pyttsx3() -> str | None:
    try:
        import pyttsx3  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        return "pyttsx3 not installed (pip install pyttsx3)"
    if not shutil.which("ffmpeg"):
        return "ffmpeg not installed (brew install ffmpeg)"
    return None


def _generate_pyttsx3(text: str, tmp_dir: str, uid: str) -> str | None:
    wav_path = os.path.join(tmp_dir, f"tg_{uid}.wav")
    ogg_path = os.path.join(tmp_dir, f"tg_{uid}.ogg")
    timeout = _estimate_timeout(len(text))
    try:
        _update_status(uid, "encoding", engine="pyttsx3", est_timeout=timeout)
        _log(f"TTS [{uid}] encoding with pyttsx3 (est. timeout: {timeout}s)")
        import pyttsx3
        engine = pyttsx3.init()
        engine.save_to_file(text, wav_path)
        engine.runAndWait()
        _update_status(uid, "converting", engine="pyttsx3")
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus", "-b:a", "64000", ogg_path],
            check=True, timeout=120, capture_output=True,
        )
        _cleanup(wav_path)
        return ogg_path
    except Exception as exc:
        _log(f"TTS [{uid}] pyttsx3 failed: {exc}")
        _cleanup(wav_path)
        _cleanup(ogg_path)
        return None


# Register built-in engines (order = default fallback priority)
_register_tts("say", _check_say, _generate_say)
_register_tts("openai", _check_openai, _generate_openai)
_register_tts("pyttsx3", _check_pyttsx3, _generate_pyttsx3)


# ── Plugin interface ──────────────────────────────────────────────────────

def plugin_info() -> dict:
    return {
        "name": "telegram",
        "version": "0.4.0",
        "description": "Telegram notifications with TTS audio fallback",
    }


def configure(config: dict) -> None:
    """Called once at load time with the plugin's config section from plugins.toml."""
    global _bot_token, _chat_id, _project_name
    global _audio_threshold, _tts_engine, _voice_upload_timeout
    global _tts_openai_model, _tts_openai_voice, _tts_openai_api_key

    _project_name = config.get("project_name", "") or os.path.basename(os.getcwd())
    _audio_threshold = int(config.get("audio_threshold", 8192))
    _tts_engine = config.get("tts_engine", "say")
    _voice_upload_timeout = int(config.get("voice_upload_timeout", 300))

    # OpenAI TTS config
    _tts_openai_model = config.get("tts_openai_model", "tts-1")
    _tts_openai_voice = config.get("tts_openai_voice", "alloy")
    api_key_env = config.get("tts_openai_api_key_env", "OPENAI_API_KEY")
    _tts_openai_api_key = config.get("tts_openai_api_key") or os.environ.get(api_key_env)

    # Credentials
    _bot_token = config.get("bot_token")
    _chat_id = config.get("chat_id")

    if not _bot_token:
        token_env = config.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
        _bot_token = os.environ.get(token_env)
    if not _chat_id:
        chat_env = config.get("chat_id_env", "TELEGRAM_CHAT_ID")
        _chat_id = os.environ.get(chat_env)

    if not _bot_token or not _chat_id:
        missing = []
        if not _bot_token:
            missing.append("bot_token / " + config.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
        if not _chat_id:
            missing.append("chat_id / " + config.get("chat_id_env", "TELEGRAM_CHAT_ID"))
        print(
            f"[telegram] WARNING: credentials not found: {', '.join(missing)} — plugin disabled",
            file=sys.stderr,
        )
        _bot_token = None
        _chat_id = None


def on_inbound(response_text: str, request_summary: dict) -> str | None:
    """Fire-and-forget Telegram notification after each Claude response."""
    if not _bot_token or not _chat_id:
        return None

    user_text = request_summary.get("user_text", "")
    project = _project_name
    header = f'Project: {project}\nPrompt: "{user_text}"'

    # Capture module-level state into locals before spawning.
    token = _bot_token
    chat_id = _chat_id
    threshold = _audio_threshold
    engine = _tts_engine
    upload_timeout = _voice_upload_timeout

    def _send() -> None:
        diagnostics: list[str] = []

        # Long response → try TTS audio
        if len(response_text) > threshold and engine != "none":
            _log(f"Response {len(response_text)} chars > threshold {threshold}, trying TTS ({engine})...")
            ogg_path, diagnostics = _tts_to_ogg(response_text, engine)
            if ogg_path is not None:
                size_mb = os.path.getsize(ogg_path) / (1024 * 1024)
                uid = _tts_status.get("uid", "?")
                _update_status(uid, "uploading", size_mb=round(size_mb, 1))
                _log(f"TTS [{uid}] OGG ready ({size_mb:.1f} MB, {_tts_status.get('elapsed', 0)}s total), uploading (timeout={upload_timeout}s)...")
                try:
                    caption = header
                    if len(caption) > MAX_TG_CAPTION:
                        caption = caption[:MAX_TG_CAPTION - 3] + "..."
                    _send_voice(token, chat_id, ogg_path, caption, upload_timeout)
                    _update_status(uid, "done")
                    _log(f"TTS [{uid}] sent successfully ({_tts_status.get('elapsed', 0)}s total)")
                    _clear_status()
                    return
                except Exception as exc:
                    diagnostics.append(f"upload failed ({exc})")
                    _update_status(uid, "failed", error=str(exc))
                    _log(f"TTS [{uid}] upload failed: {exc}. Falling back to text.")
                finally:
                    _cleanup(ogg_path)
            else:
                uid = _tts_status.get("uid", "?")
                _update_status(uid, "failed", error="; ".join(diagnostics))
                _log(f"TTS [{uid}] all engines failed ({_tts_status.get('elapsed', 0)}s total). Falling back to text.")

            _clear_status()
            diag_note = "; ".join(diagnostics)
            chunks = _split_message(response_text, project, user_text, diag_note)
        else:
            chunks = _split_message(response_text, project, user_text)

        tg_url = f"https://api.telegram.org/bot{token}/sendMessage"
        for chunk in chunks:
            try:
                data = json.dumps({"chat_id": chat_id, "text": chunk}).encode()
                req = urllib.request.Request(
                    tg_url, data=data, headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as exc:
                _log(f"ERROR: {exc}")

    threading.Thread(target=_send, daemon=True).start()
    return None


# ── Message splitting ─────────────────────────────────────────────────────

def _split_message(
    response: str,
    project: str,
    user_text: str,
    tts_diagnostic: str | None = None,
) -> list[str]:
    """Split response into Telegram-safe chunks with per-chunk headers.

    First chunk:  Project: <name>\\nPrompt: "<full prompt>"\\n[diagnostic]\\n<text>
    Subsequent:   Project: <name> [2/N]\\n<text>
    """
    first_header = f'Project: {project}\nPrompt: "{user_text}"\n'
    if tts_diagnostic:
        first_header += f"Audio unavailable: {tts_diagnostic}. Sending as text.\n"

    first_body_max = MAX_TG_LENGTH - len(first_header)

    if first_body_max >= len(response):
        return [first_header + response]

    # Pre-scan to figure out total chunk count
    raw_chunks: list[str] = []
    rest = response
    raw_chunks.append(rest[:first_body_max])
    rest = rest[first_body_max:]
    cont_header_template = f"Project: {project} [00/00]\n"
    cont_body_max = MAX_TG_LENGTH - len(cont_header_template)
    while rest:
        raw_chunks.append(rest[:cont_body_max])
        rest = rest[cont_body_max:]

    total = len(raw_chunks)
    result = [first_header + raw_chunks[0]]
    for i, body in enumerate(raw_chunks[1:], start=2):
        result.append(f"Project: {project} [{i}/{total}]\n{body}")
    return result


# ── TTS orchestration ─────────────────────────────────────────────────────

def _tts_to_ogg(text: str, engine_name: str) -> tuple[str | None, list[str]]:
    """Try TTS engines in priority order. Returns (ogg_path, diagnostics).

    The preferred engine is tried first, then remaining engines as fallbacks.
    Diagnostics collects a human-readable reason from each failed engine.
    """
    global _tts_status
    diagnostics: list[str] = []
    uid = uuid.uuid4().hex[:8]
    tmp_dir = tempfile.gettempdir()

    # Initialize status tracking
    _tts_status = {"uid": uid, "stage": "check", "start_mono": time.monotonic(), "elapsed": 0}

    # Build ordered list: preferred engine first, then others
    ordered = _get_engine_order(engine_name)

    for eng in ordered:
        # Pre-flight check
        _update_status(uid, "check", engine=eng["name"])
        check_err = eng["check"]()
        if check_err:
            diagnostics.append(f"{eng['name']}: {check_err}")
            _log(f"TTS [{uid}] {eng['name']} check failed: {check_err}")
            continue

        # Try generation
        ogg_path = eng["generate"](text, tmp_dir, uid)
        if ogg_path:
            return ogg_path, diagnostics
        diagnostics.append(f"{eng['name']}: generation failed")

    return None, diagnostics


def _get_engine_order(preferred: str) -> list[dict]:
    """Return registry entries with *preferred* first, others after."""
    first = [e for e in _TTS_REGISTRY if e["name"] == preferred]
    rest = [e for e in _TTS_REGISTRY if e["name"] != preferred]
    return first + rest


# ── Telegram voice upload ─────────────────────────────────────────────────

def _send_voice(token: str, chat_id: str, ogg_path: str, caption: str, timeout: int = 300) -> None:
    """Send an OGG Opus file as a Telegram voice message via multipart POST."""
    boundary = uuid.uuid4().hex
    url = f"https://api.telegram.org/bot{token}/sendVoice"

    with open(ogg_path, "rb") as f:
        audio_data = f.read()

    parts: list[bytes] = []
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n".encode()
    )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n'
        f"{caption}\r\n".encode()
    )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="voice"; filename="voice.ogg"\r\n'
        f"Content-Type: audio/ogg\r\n\r\n".encode()
        + audio_data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    urllib.request.urlopen(req, timeout=timeout)


# ── Helpers ───────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[telegram] {msg}", file=sys.stderr)


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
