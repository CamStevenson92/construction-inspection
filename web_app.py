"""
Flask web backend for the Site Inspection Photo Processor.
Run with:  python web_app.py
Then open: http://localhost:5000
"""

import io
import json
import logging
import os
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, abort, jsonify, render_template, request, send_file

try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from models import PhotoData
from processors import (
    compute_phash,
    detect_duplicates,
    extract_photo_data,
    fetch_weather,
)
from report_generator import generate_report

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32)

UPLOAD_ROOT = Path("web_uploads")
REPORT_ROOT = Path("web_reports")
UPLOAD_ROOT.mkdir(exist_ok=True)
REPORT_ROOT.mkdir(exist_ok=True)

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".webp"}
THUMB_W, THUMB_H = 200, 200
SESSION_TTL_HOURS = 6

# ---------------------------------------------------------------------------
# In-memory session store
# ---------------------------------------------------------------------------

class SessionState:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.upload_dir = UPLOAD_ROOT / session_id
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.photos: List[PhotoData] = []
        self.total_files: int = 0
        self.processed: int = 0
        self.errors: List[str] = []
        self.processing: bool = False
        self.thumb_cache: Dict[int, bytes] = {}
        self.created_at: float = time.time()
        self._lock = threading.Lock()

    def to_status(self) -> dict:
        return {
            "total": self.total_files,
            "processed": self.processed,
            "processing": self.processing,
            "errors": self.errors,
        }

    def photo_to_dict(self, photo: PhotoData, idx: int) -> dict:
        return {
            "idx": idx,
            "filename": photo.filename,
            "datetime": photo.datetime_label,
            "coords": photo.coords_label,
            "direction": photo.direction_label,
            "altitude": photo.altitude_label,
            "camera": f"{photo.make} {photo.model}".strip(),
            "weather": photo.weather.summary() if photo.weather else None,
            "has_gps": photo.has_gps,
            "has_direction": photo.has_direction,
            "is_duplicate": photo.is_duplicate,
            "similar_to": photo.similar_to or None,
            "what_inspected": photo.what_inspected,
            "issues_found": photo.issues_found,
            "actions_required": photo.actions_required,
        }


SESSIONS: Dict[str, SessionState] = {}
SESSIONS_LOCK = threading.Lock()


def get_session(session_id: str) -> Optional[SessionState]:
    with SESSIONS_LOCK:
        return SESSIONS.get(session_id)


def require_session(session_id: str) -> SessionState:
    s = get_session(session_id)
    if s is None:
        abort(404, "Session not found")
    return s


# ---------------------------------------------------------------------------
# Background session cleanup (removes sessions older than TTL)
# ---------------------------------------------------------------------------

def _cleanup_loop():
    while True:
        time.sleep(1800)
        cutoff = time.time() - SESSION_TTL_HOURS * 3600
        with SESSIONS_LOCK:
            stale = [sid for sid, s in SESSIONS.items() if s.created_at < cutoff]
        for sid in stale:
            with SESSIONS_LOCK:
                s = SESSIONS.pop(sid, None)
            if s:
                shutil.rmtree(s.upload_dir, ignore_errors=True)
            rdir = REPORT_ROOT / sid
            if rdir.exists():
                shutil.rmtree(rdir, ignore_errors=True)
            log.info("Cleaned up stale session %s", sid)


threading.Thread(target=_cleanup_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Background photo processing
# ---------------------------------------------------------------------------

def _process_photos(state: SessionState, filepaths: List[Path]):
    state.processing = True
    photos: List[PhotoData] = []

    for fp in filepaths:
        try:
            photo = extract_photo_data(str(fp))
            photo.phash = compute_phash(str(fp))
            if photo.has_gps:
                photo.weather = fetch_weather(
                    photo.latitude, photo.longitude, photo.datetime_taken
                )
            photos.append(photo)
        except Exception as e:
            log.exception("Error processing %s", fp.name)
            with state._lock:
                state.errors.append(f"{fp.name}: {e}")

        with state._lock:
            state.processed += 1

    detect_duplicates(photos)

    with state._lock:
        state.photos = photos
        state.processing = False

    log.info("Session %s: processed %d photos", state.session_id, len(photos))


# ---------------------------------------------------------------------------
# Thumbnail helper
# ---------------------------------------------------------------------------

def _make_thumb(filepath: str) -> bytes:
    from PIL import Image, ImageOps
    img = Image.open(filepath)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _make_full(filepath: str, max_w: int = 1200, max_h: int = 900) -> bytes:
    from PIL import Image, ImageOps
    img = Image.open(filepath)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/session", methods=["POST"])
def create_session():
    sid = str(uuid.uuid4())
    state = SessionState(sid)
    with SESSIONS_LOCK:
        SESSIONS[sid] = state
    log.info("Created session %s", sid)
    return jsonify({"session_id": sid})


@app.route("/api/upload/<session_id>", methods=["POST"])
def upload_photos(session_id: str):
    state = require_session(session_id)
    files = request.files.getlist("photos")

    if not files:
        return jsonify({"error": "No files provided"}), 400

    saved: List[Path] = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            continue
        # Use a safe unique name to avoid collisions
        safe_name = f"{uuid.uuid4().hex[:8]}_{f.filename}"
        dest = state.upload_dir / safe_name
        f.save(str(dest))
        # Store original filename in PhotoData later
        saved.append(dest)

    if not saved:
        return jsonify({"error": "No supported image files found"}), 400

    with state._lock:
        state.total_files = len(saved)
        state.processed = 0
        state.errors = []
        state.photos = []
        state.thumb_cache = {}

    t = threading.Thread(target=_process_photos, args=(state, saved), daemon=True)
    t.start()

    return jsonify({"queued": len(saved)})


@app.route("/api/status/<session_id>")
def get_status(session_id: str):
    state = require_session(session_id)
    with state._lock:
        status = state.to_status()
    return jsonify(status)


@app.route("/api/photos/<session_id>")
def get_photos(session_id: str):
    state = require_session(session_id)
    with state._lock:
        photos_json = [state.photo_to_dict(p, i) for i, p in enumerate(state.photos)]
    return jsonify(photos_json)


@app.route("/api/photo/<session_id>/<int:idx>/thumb")
def get_thumb(session_id: str, idx: int):
    state = require_session(session_id)
    with state._lock:
        if idx >= len(state.photos):
            abort(404)
        cached = state.thumb_cache.get(idx)
        filepath = state.photos[idx].file_path

    if cached:
        return send_file(io.BytesIO(cached), mimetype="image/jpeg")

    data = _make_thumb(filepath)
    with state._lock:
        state.thumb_cache[idx] = data
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/photo/<session_id>/<int:idx>/image")
def get_image(session_id: str, idx: int):
    state = require_session(session_id)
    with state._lock:
        if idx >= len(state.photos):
            abort(404)
        filepath = state.photos[idx].file_path

    data = _make_full(filepath)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/notes/<session_id>/<int:idx>", methods=["POST"])
def save_notes(session_id: str, idx: int):
    state = require_session(session_id)
    body = request.get_json(force=True) or {}
    with state._lock:
        if idx >= len(state.photos):
            abort(404)
        photo = state.photos[idx]
        photo.what_inspected = body.get("what_inspected", "")
        photo.issues_found = body.get("issues_found", "")
        photo.actions_required = body.get("actions_required", "")
    return jsonify({"ok": True})


@app.route("/api/report/<session_id>", methods=["POST"])
def generate_report_route(session_id: str):
    state = require_session(session_id)
    body = request.form

    template_file = request.files.get("template")
    template_path = None

    report_dir = REPORT_ROOT / session_id
    report_dir.mkdir(parents=True, exist_ok=True)

    if template_file and template_file.filename:
        template_path = str(report_dir / "template.docx")
        template_file.save(template_path)

    site_name = body.get("site_name", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    import re
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", site_name)[:40] or "inspection"
    docx_name = f"{slug}_{ts}.docx"
    docx_path = str(report_dir / docx_name)

    with state._lock:
        photos = list(state.photos)

    try:
        generate_report(
            photos=photos,
            output_path=docx_path,
            template_path=template_path,
            site_name=site_name,
            project_number=body.get("project_number", ""),
            inspector_name=body.get("inspector_name", ""),
            site_address=body.get("site_address", ""),
        )
    except Exception as e:
        log.exception("Report generation failed")
        return jsonify({"error": str(e)}), 500

    # Save metadata JSON
    meta_path = str(report_dir / "photo_metadata_log.json")
    with state._lock:
        entries = [state.photo_to_dict(p, i) for i, p in enumerate(state.photos)]
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

    # Build ZIP: report + photos
    zip_name = f"{slug}_{ts}_full_report.zip"
    zip_path = str(report_dir / zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(docx_path, docx_name)
        zf.write(meta_path, "photo_metadata_log.json")
        with state._lock:
            for p in state.photos:
                if os.path.isfile(p.file_path):
                    zf.write(p.file_path, f"photos/{p.filename}")

    return jsonify({
        "download_url": f"/api/download/{session_id}/{zip_name}",
        "docx_url": f"/api/download/{session_id}/{docx_name}",
        "filename": zip_name,
    })


@app.route("/api/download/<session_id>/<filename>")
def download_file(session_id: str, filename: str):
    # Safety: only allow filenames within the session report dir
    report_dir = REPORT_ROOT / session_id
    target = (report_dir / filename).resolve()
    if not str(target).startswith(str(report_dir.resolve())):
        abort(403)
    if not target.exists():
        abort(404)
    return send_file(str(target), as_attachment=True, download_name=filename)


@app.route("/api/session/<session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    with SESSIONS_LOCK:
        state = SESSIONS.pop(session_id, None)
    if state:
        shutil.rmtree(state.upload_dir, ignore_errors=True)
    rdir = REPORT_ROOT / session_id
    if rdir.exists():
        shutil.rmtree(rdir, ignore_errors=True)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Voice transcription → AI field parsing
# ---------------------------------------------------------------------------

@app.route("/api/config")
def get_config():
    """Tell the client whether AI parsing is available."""
    return jsonify({
        "ai_available": bool(_ANTHROPIC_AVAILABLE and os.environ.get("ANTHROPIC_API_KEY"))
    })


@app.route("/api/transcribe", methods=["POST"])
def transcribe_voice():
    """
    Accept a speech transcript and parse it into the three inspection fields
    using Claude. Falls back to dumping text into what_inspected if no key.
    """
    body = request.get_json(force=True) or {}
    text = body.get("transcript", "").strip()
    if not text:
        return jsonify({"error": "No transcript provided"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if _ANTHROPIC_AVAILABLE and api_key:
        try:
            result = _parse_with_claude(text, api_key)
            result["ai_parsed"] = True
            return jsonify(result)
        except Exception as e:
            log.exception("Claude parse failed, falling back")

    # Fallback: put everything in what_inspected
    return jsonify({
        "what_inspected": text,
        "issues_found": "",
        "actions_required": "",
        "ai_parsed": False,
    })


def _parse_with_claude(text: str, api_key: str) -> dict:
    client = _anthropic.Anthropic(api_key=api_key)
    prompt = (
        "You are processing a construction site inspection voice note recorded in the field.\n"
        "Parse the transcription below into exactly three categories:\n"
        "1. what_inspected — what element or area was being looked at\n"
        "2. issues_found — defects, non-conformances, damage, safety concerns\n"
        "3. actions_required — what the contractor must do to resolve the issues\n\n"
        "Rules:\n"
        "- Return ONLY valid JSON with those three keys, no other text.\n"
        "- Use the inspector's exact words where possible, clean up speech artefacts.\n"
        "- If a category has no relevant content leave it as an empty string.\n\n"
        f'Transcription: """{text}"""\n\nJSON:'
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    content = message.content[0].text.strip()
    import re
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError("No JSON in Claude response")


# ---------------------------------------------------------------------------
# Dev server entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import webbrowser
    print("\n" + "=" * 60)
    print("  Site Inspection Photo Processor — Web App")
    print("  Open in your browser: http://localhost:5000")
    print("  On same Wi-Fi network: http://<your-ip>:5000")
    print("  Press Ctrl+C to stop")
    print("=" * 60 + "\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
