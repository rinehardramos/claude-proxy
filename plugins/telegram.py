"""Telegram notification plugin for claude-proxy.

Sends Claude's full response to a Telegram chat, splitting into chunks
if needed or converting to a voice message via TTS for very long responses.
Stdlib only (+ subprocess for TTS).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from html import escape as _esc

from pathlib import Path

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

# ── Callback poller state ────────────────────────────────────────────────

HOOK_DIR = Path("~/.claude/claude-proxy/telegram-hook").expanduser()
_poller_thread: threading.Thread | None = None
_poller_stop = threading.Event()

# ── Reply / mute state ──────────────────────────────────────────────────

_pending_replies: list[str] = []
_pending_replies_lock = threading.Lock()
_muted = False
_waiting_for_reply = False  # True after user taps Reply button, awaiting text
_approval_mode = "ask"  # "ask" | "auto-approve" | "auto-deny"
_MODE_FILE = HOOK_DIR / "mode"  # shared with telegram_approve.py hook script


def _write_mode(mode: str) -> None:
    """Persist approval mode to file for the hook script to read."""
    global _approval_mode
    _approval_mode = mode
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    _MODE_FILE.write_text(mode)


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

    _project_name = config.get("project_name", "")  # explicit override only; dynamic cwd used at runtime
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
        return

    # Start callback poller for inline keyboard responses (opt-in)
    if config.get("approval_poller", "").lower() in ("true", "1", "yes", "on"):
        _start_poller()


# ── Option extraction ────────────────────────────────────────────────────

_NUMBERED_OPTION_RE = re.compile(r"^\s*(\d+)\.\s+(.+)$", re.MULTILINE)


def _extract_options(text: str) -> list[str] | None:
    """Extract numbered options from an interactive prompt at the end of text.

    Only matches when:
    - 2-4 short options (< 80 chars each)
    - Options appear in the last 6 lines of text
    - Options are consecutive numbered lines (no gaps)
    """
    lines = text.rstrip().split("\n")
    tail = lines[-6:] if len(lines) > 6 else lines

    matches = []
    for line in tail:
        m = _NUMBERED_OPTION_RE.match(line)
        if m:
            label = m.group(2).strip()
            if len(label) < 80:
                matches.append(label)
        elif matches:
            # Non-matching line after matches started = break in sequence
            break

    if len(matches) < 2 or len(matches) > 4:
        return None
    return matches


def on_inbound(response_text: str, request_summary: dict) -> str | None:
    """Fire-and-forget Telegram notification after each Claude response."""
    if not _bot_token or not _chat_id:
        return None

    if _muted:
        return None

    if not response_text or not response_text.strip():
        return None

    user_text = request_summary.get("user_text", "")
    cwd = request_summary.get("cwd", "")
    project = os.path.basename(cwd) if cwd else (_project_name or "(unknown project)")
    header = f'<b>{_esc(project)}</b>\n<blockquote>{_esc(user_text)}</blockquote>'

    # Capture module-level state into locals before spawning.
    token = _bot_token
    chat_id = _chat_id
    threshold = _audio_threshold
    engine = _tts_engine
    upload_timeout = _voice_upload_timeout

    def _send() -> None:
        try:
            _log(f"sending notification ({len(response_text)} chars)")
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

            # Detect numbered options for inline buttons
            options = _extract_options(response_text)

            tg_url = f"https://api.telegram.org/bot{token}/sendMessage"
            for i, chunk in enumerate(chunks):
                payload: dict = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}

                # First chunk gets inline keyboard
                if i == 0:
                    if options:
                        # Render numbered options as buttons
                        keyboard = [[{"text": opt, "callback_data": f"option:{j}"}]
                                     for j, opt in enumerate(options)]
                    else:
                        keyboard = [[{"text": "\U0001f4ac Reply", "callback_data": "reply:0"}]]
                    payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})

                try:
                    data = json.dumps(payload).encode()
                    req = urllib.request.Request(
                        tg_url, data=data, headers={"Content-Type": "application/json"},
                    )
                    urllib.request.urlopen(req, timeout=10)
                except Exception as exc:
                    _log(f"ERROR: {exc}")
        except Exception as exc:
            _log(f"FATAL in _send thread: {exc}")

    threading.Thread(target=_send, daemon=True).start()
    return None


def on_outbound(payload: dict) -> dict | None:
    """Inject pending Telegram replies into the outbound request."""
    with _pending_replies_lock:
        if not _pending_replies:
            return None
        replies = list(_pending_replies)
        _pending_replies.clear()

    import copy
    payload = copy.deepcopy(payload)

    # Build injection text
    parts = [f"User replied via Telegram: {r}" for r in replies]
    injection = "<system-reminder>\n" + "\n".join(parts) + "\n</system-reminder>"

    # Inject into the last user message
    messages = payload.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                msg["content"] = content + "\n" + injection
            elif isinstance(content, list):
                content.append({"type": "text", "text": injection})
            break

    _log(f"injected {len(replies)} reply(s) into outbound request")
    return payload


# ── Markdown → Telegram HTML ─────────────────────────────────────────────

_MD_TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n){2,})",  # 2+ consecutive pipe-delimited lines
    re.MULTILINE,
)
_MD_SEPARATOR_RE = re.compile(r"^[ \t]*\|[\s|:\-]+\|[ \t]*$")


def _md_to_tg_html(text: str) -> str:
    """Convert markdown elements to Telegram-compatible HTML.

    - Tables → <pre> blocks (monospace keeps columns aligned)
    - Everything else → HTML-escaped and placed as-is
    """
    parts: list[str] = []
    last_end = 0

    for m in _MD_TABLE_RE.finditer(text):
        # Text before this table
        before = text[last_end:m.start()]
        if before:
            parts.append(_esc(before))

        # Process table: strip separator rows, keep data rows in <pre>
        table_lines = m.group(1).rstrip("\n").split("\n")
        data_lines = [ln for ln in table_lines if not _MD_SEPARATOR_RE.match(ln)]
        parts.append(f"<pre>{_esc(chr(10).join(data_lines))}</pre>")
        last_end = m.end()

    # Remaining text after last table
    tail = text[last_end:]
    if tail:
        parts.append(_esc(tail))

    return "".join(parts)


# ── Message splitting ─────────────────────────────────────────────────────

def _split_message(
    response: str,
    project: str,
    user_text: str,
    tts_diagnostic: str | None = None,
) -> list[str]:
    """Split response into Telegram-safe HTML chunks.

    First chunk:  bold header + blockquote response
    Subsequent:   bold project [i/N] + blockquote continuation
    """
    esc_project = _esc(project)
    esc_prompt = _esc(user_text)
    first_header = f'<b>{esc_project}</b>\n<blockquote>{esc_prompt}</blockquote>\n'
    if tts_diagnostic:
        first_header += f"Audio unavailable: {_esc(tts_diagnostic)}. Sending as text.\n"

    # Wrap/close tags add to overhead per chunk
    bq_open = "<blockquote>"
    bq_close = "</blockquote>"
    bq_overhead = len(bq_open) + len(bq_close)

    esc_response = _md_to_tg_html(response)
    first_body_max = MAX_TG_LENGTH - len(first_header) - bq_overhead

    if first_body_max >= len(esc_response):
        return [f"{first_header}{bq_open}{esc_response}{bq_close}"]

    # Pre-scan to figure out total chunk count
    raw_chunks: list[str] = []
    rest = esc_response
    raw_chunks.append(rest[:first_body_max])
    rest = rest[first_body_max:]
    cont_header_template = f"<b>{esc_project} [00/00]</b>\n"
    cont_body_max = MAX_TG_LENGTH - len(cont_header_template) - bq_overhead
    while rest:
        raw_chunks.append(rest[:cont_body_max])
        rest = rest[cont_body_max:]

    total = len(raw_chunks)
    result = [f"{first_header}{bq_open}{raw_chunks[0]}{bq_close}"]
    for i, body in enumerate(raw_chunks[1:], start=2):
        result.append(f"<b>{esc_project} [{i}/{total}]</b>\n{bq_open}{body}{bq_close}")
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
        f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
        f"HTML\r\n".encode()
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


# ── Callback Poller ──────────────────────────────────────────────────────

def _get_updates(token: str, offset: int, timeout: int = 30) -> list[dict]:
    """Long-poll Telegram getUpdates for new updates."""
    url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout={timeout}"
    resp = urllib.request.urlopen(url, timeout=timeout + 10)
    data = json.loads(resp.read())
    return data.get("result", [])


def _answer_callback_query(token: str, query_id: str, text: str) -> None:
    """Acknowledge a callback button press."""
    data = json.dumps({"callback_query_id": query_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _send_text(token: str, chat_id: str, text: str, reply_to: int | None = None) -> None:
    """Send a simple text message."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _handle_text_message(msg: dict, token: str, chat_id: str) -> None:
    """Handle incoming text messages: replies, /mute command."""
    global _muted, _waiting_for_reply, _csm_waiting_reply_pid

    text = msg.get("text", "").strip()
    if not text:
        return

    # /mute toggle
    if text.lower() in ("/mute", "/mute_on", "/mute_off"):
        if text.lower() == "/mute_off":
            _muted = False
        elif text.lower() == "/mute_on":
            _muted = True
        else:
            _muted = not _muted
        status = "Muted \U0001f507" if _muted else "Unmuted \U0001f50a"
        _send_text(token, chat_id, status)
        _log(f"mute toggled: {_muted}")
        return

    # /mode command
    if text.lower().startswith("/mode"):
        _valid_modes = {"auto-approve", "ask", "auto-deny"}
        parts = text.split(None, 1)
        if len(parts) == 2 and parts[1].lower().strip() in _valid_modes:
            mode = parts[1].lower().strip()
            _write_mode(mode)
            icons = {"auto-approve": "\u2705", "ask": "\u2753", "auto-deny": "\u274c"}
            _send_text(token, chat_id, f"{icons.get(mode, '')} Mode: {mode}")
            _log(f"mode set: {mode}")
        else:
            _send_text(token, chat_id,
                       f"Current: {_approval_mode}\n"
                       f"Usage: /mode auto-approve | ask | auto-deny")
        return

    # Session-monitor text reply: user typed text after tapping 💬 Reply
    _log(f"text msg received: csm_pid={_csm_waiting_reply_pid} waiting_reply={_waiting_for_reply}")
    if _csm_waiting_reply_pid is not None:
        pid = _csm_waiting_reply_pid
        _csm_waiting_reply_pid = None
        _CSM_INBOX.mkdir(parents=True, exist_ok=True)
        fname = _CSM_INBOX / f"{time.time_ns()}.json"
        fname.write_text(json.dumps({"data": f"text:{pid}:{text}"}))
        _send_text(token, chat_id, "✅ Reply sent")
        _log(f"session-monitor reply forwarded ({len(text)} chars, pid={pid})")
        return

    # Waiting for reply text after user tapped Reply button
    if _waiting_for_reply:
        _waiting_for_reply = False
        with _pending_replies_lock:
            _pending_replies.append(text)
        _send_text(token, chat_id, "\u2705 Reply queued")
        _log(f"reply queued ({len(text)} chars)")
        return

    # Reply-to-message: user used Telegram's native reply on a bot message
    reply_to = msg.get("reply_to_message", {})
    if reply_to.get("from", {}).get("is_bot"):
        with _pending_replies_lock:
            _pending_replies.append(text)
        _send_text(token, chat_id, "\u2705 Reply queued")
        _log(f"reply-to queued ({len(text)} chars)")
        return


def _handle_option_callback(cb: dict, token: str, chat_id: str, option_index: str) -> None:
    """Handle an option button press — inject the selected option."""
    query_id = cb.get("id", "")
    msg = cb.get("message", {})
    original_text = msg.get("text", "")

    # Find the option label from the inline keyboard
    keyboard = msg.get("reply_markup", {}).get("inline_keyboard", [])
    label = f"Option {option_index}"
    try:
        idx = int(option_index)
        if 0 <= idx < len(keyboard) and keyboard[idx]:
            label = keyboard[idx][0].get("text", label)
    except (ValueError, IndexError):
        pass

    with _pending_replies_lock:
        _pending_replies.append(label)

    _answer_callback_query(token, query_id, f"Selected: {label[:40]}")

    # Update button to show selection
    selected_keyboard = {"inline_keyboard": [[
        {"text": f"\u2705 {label}", "callback_data": "noop:selected"},
    ]]}
    data = json.dumps({
        "chat_id": chat_id,
        "message_id": msg.get("message_id"),
        "text": original_text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(selected_keyboard),
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/editMessageText",
        data=data, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

    _log(f"option selected: {label[:40]}")


def _handle_reply_callback(cb: dict, token: str, chat_id: str) -> None:
    """Handle Reply button press — set waiting state."""
    global _waiting_for_reply
    query_id = cb.get("id", "")
    _waiting_for_reply = True
    _answer_callback_query(token, query_id, "Type your reply:")
    _send_text(token, chat_id, "\U0001f4ac Type your reply:")
    _log("waiting for reply text")


def _edit_message_decided(
    token: str, chat_id: str, message_id: int,
    original_text: str, decision: str,
) -> None:
    """Update the message: replace action buttons with a decision-state button."""
    icon = "\u2705" if decision == "allow" else "\u274c"  # green check / red X
    label = "Approved" if decision == "allow" else "Denied"

    # Replace Approve/Deny buttons with a single decision-indicator button
    decided_keyboard = {"inline_keyboard": [[
        {"text": f"{icon} {label}", "callback_data": "noop:decided"},
    ]]}

    data = json.dumps({
        "chat_id": chat_id,
        "message_id": message_id,
        "text": original_text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(decided_keyboard),
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/editMessageText",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


_CSM_INBOX = Path.home() / ".claude" / "session-monitor-inbox"
_csm_waiting_reply_pid: str | None = None  # PID waiting for text reply via session monitor


def _forward_to_session_monitor(cb: dict, cb_data: str, query_id: str,
                                 token: str, chat_id: str) -> None:
    """Write a session-monitor callback to the shared inbox directory,
    then edit the Telegram message to confirm the selection and remove buttons."""
    global _csm_waiting_reply_pid
    try:
        action = cb_data.split(":")[0]
        msg    = cb.get("message", {})
        msg_id = msg.get("message_id")
        orig   = msg.get("text", "")

        # "csmreply" action: don't write to inbox yet — wait for user's text
        if action == "csmreply":
            parts = cb_data.split(":", 1)
            _csm_waiting_reply_pid = parts[1] if len(parts) > 1 else None
            _answer_callback_query(token, query_id, "Type your reply")
            # Remove buttons from original message (don't edit text — avoids 400)
            if msg_id:
                try:
                    data = json.dumps({
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "reply_markup": json.dumps({"inline_keyboard": []}),
                    }).encode()
                    req = urllib.request.Request(
                        f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
                        data=data,
                        headers={"Content-Type": "application/json"},
                    )
                    urllib.request.urlopen(req, timeout=10).read()
                except Exception:
                    pass
            _send_text(token, chat_id, "💬 Type your reply:")
            _log(f"session-monitor: waiting for reply text (pid={_csm_waiting_reply_pid})")
            return

        # All other actions: write to inbox immediately
        _CSM_INBOX.mkdir(parents=True, exist_ok=True)
        fname = _CSM_INBOX / f"{time.time_ns()}.json"
        fname.write_text(json.dumps({"data": cb_data}))

        # Acknowledge button press with brief toast
        toast = {"ans": "✓ Sent", "dismiss": "Dismissed", "continue": "▶ Continuing"}.get(action, "✓")
        _answer_callback_query(token, query_id, toast)

        # Edit message: replace buttons with confirmation line
        if msg_id:
            label = {"ans": "✅ Answer sent", "dismiss": "🔕 Dismissed", "continue": "▶ Continuing"}.get(action, "✅ Done")
            edited = f"{orig}\n\n<i>{label}</i>"
            data = json.dumps({
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": edited,
                "parse_mode": "HTML",
                "reply_markup": json.dumps({"inline_keyboard": []}),
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/editMessageText",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10).read()

    except Exception as e:
        _log(f"session-monitor forward failed: {e}")


def _handle_callback(cb: dict, token: str, chat_id: str) -> None:
    """Process a callback_query from an inline keyboard button press."""
    cb_data = cb.get("data", "")
    query_id = cb.get("id", "")

    # Expected format: "approve:<decision_id>" or "deny:<decision_id>"
    if ":" not in cb_data:
        return

    action, decision_id = cb_data.split(":", 1)

    # Forward session-monitor callbacks via file-based IPC
    if action in ("ans", "dismiss", "continue", "csmreply", "wakeup"):
        _forward_to_session_monitor(cb, cb_data, query_id, token, chat_id)
        return

    # Route to specialized handlers
    if action == "reply":
        _handle_reply_callback(cb, token, chat_id)
        return
    if action == "option":
        _handle_option_callback(cb, token, chat_id, decision_id)
        return

    if action == "noop":
        _answer_callback_query(token, query_id, "Already decided")
        # Remove the button entirely on re-tap
        msg = cb.get("message", {})
        msg_id = msg.get("message_id")
        if msg_id:
            data = json.dumps({
                "chat_id": chat_id,
                "message_id": msg_id,
                "reply_markup": json.dumps({"inline_keyboard": []}),
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass
        return
    if action not in ("approve", "deny"):
        return

    pending_path = HOOK_DIR / "pending" / f"{decision_id}.json"
    decided_path = HOOK_DIR / "decided" / f"{decision_id}.json"

    if not pending_path.exists():
        _answer_callback_query(token, query_id, "Decision expired or not found")
        return

    decision = "allow" if action == "approve" else "deny"
    decided_path.write_text(json.dumps({
        "decision": decision,
        "decided_at": time.time(),
    }))

    # Remove the pending file
    try:
        pending_path.unlink()
    except OSError:
        pass

    # Acknowledge the button press
    label = "Approved" if decision == "allow" else "Denied"
    _answer_callback_query(token, query_id, label)

    # Update message with decision indicator and remove buttons
    msg = cb.get("message", {})
    msg_id = msg.get("message_id")
    original_text = msg.get("text", "")
    if msg_id:
        _edit_message_decided(token, chat_id, msg_id, original_text, decision)

    _log(f"callback: {action} decision_id={decision_id[:8]}...")


def _edit_message_expired(token: str, chat_id: str, message_id: int, original_text: str) -> None:
    """Mark a Telegram approval message as expired."""
    expired_keyboard = {"inline_keyboard": [[
        {"text": "\u23f3 Expired", "callback_data": "noop:expired"},
    ]]}
    data = json.dumps({
        "chat_id": chat_id,
        "message_id": message_id,
        "text": original_text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(expired_keyboard),
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/editMessageText",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _cleanup_stale_hook_files(token: str, chat_id: str, max_age: int = 600) -> None:
    """Remove stale files and mark expired Telegram messages."""
    now = time.time()

    # Pending files: edit Telegram message to show expired before deleting
    pending_dir = HOOK_DIR / "pending"
    if pending_dir.exists():
        for f in pending_dir.iterdir():
            if f.suffix != ".json":
                continue
            try:
                age = now - f.stat().st_mtime
                if age > max_age:
                    # Read message_id to update the Telegram message
                    try:
                        info = json.loads(f.read_text())
                        msg_id = info.get("message_id")
                        if msg_id and token and chat_id:
                            # Fetch original text isn't stored, use a generic message
                            _edit_message_expired(token, chat_id, msg_id,
                                                  info.get("_original_text", "Approval request"))
                    except (json.JSONDecodeError, OSError):
                        pass
                    f.unlink()
                    _log(f"cleaned stale pending: {f.name}")
            except OSError:
                pass

    # Decided files: just delete old ones (already consumed or orphaned)
    decided_dir = HOOK_DIR / "decided"
    if decided_dir.exists():
        for f in decided_dir.iterdir():
            if f.suffix == ".json":
                try:
                    if now - f.stat().st_mtime > max_age:
                        f.unlink()
                except OSError:
                    pass


def _poll_loop() -> None:
    """Background loop: long-poll Telegram for callback_query and message updates."""
    token = _bot_token
    chat_id = _chat_id
    if not token or not chat_id:
        return

    offset = 0
    cleanup_counter = 0

    _log("callback poller started")
    while not _poller_stop.is_set():
        try:
            updates = _get_updates(token, offset, timeout=30)
            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)
                cb = update.get("callback_query")
                if cb:
                    _handle_callback(cb, token, chat_id)
                msg = update.get("message")
                if msg and msg.get("text"):
                    _handle_text_message(msg, token, chat_id)

            # Periodic cleanup every ~10 polls
            cleanup_counter += 1
            if cleanup_counter >= 10:
                cleanup_counter = 0
                _cleanup_stale_hook_files(token, chat_id)

        except Exception as exc:
            _log(f"poller error: {exc}")
            _poller_stop.wait(5)


def _start_poller() -> None:
    """Start the callback poller daemon thread, restarting if already running."""
    global _poller_thread
    if _poller_thread is not None and _poller_thread.is_alive():
        _poller_stop.set()
        _poller_thread.join(timeout=5)
        _log("poller thread restarted (hot-reload)")
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    (HOOK_DIR / "pending").mkdir(exist_ok=True)
    (HOOK_DIR / "decided").mkdir(exist_ok=True)
    _poller_stop.clear()
    _poller_thread = threading.Thread(target=_poll_loop, daemon=True, name="tg-poller")
    _poller_thread.start()


# ── Helpers ───────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[telegram] {msg}", file=sys.stderr)


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
