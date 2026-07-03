"""Flask web UI for the admissions summary pipeline.

    python -m src.app          # then open http://127.0.0.1:5000

Upload a CSV/Excel -> the deterministic pipeline runs -> browse a descriptive
summary per applicant. No scoring or ranking; the app summarizes the data and
leaves the judgement to the reviewer. No pipeline logic lives here; this only
orchestrates src.run and renders the results.

This app renders full applicant PII (names, emails, test scores, SOP/LOR
text) with no login by default, so it ships with guardrails on by default
and opt-in escape hatches via environment variables:

  GRADAPP_SECRET_KEY    Session/flash cookie signing key. Auto-generated
                        (random, not persisted) if unset; set it explicitly
                        if flashed messages need to survive a process restart.
  GRADAPP_DEBUG         "1" enables Flask/Werkzeug debug mode (interactive
                        debugger + full tracebacks in the browser). Off by
                        default -- the debugger allows arbitrary code
                        execution if this process is ever reachable by
                        anyone else.
  GRADAPP_HOST          Bind address. Defaults to 127.0.0.1 (localhost only).
                        A non-loopback host requires GRADAPP_ALLOW_REMOTE=1
                        *and* GRADAPP_PASSWORD -- the app refuses to start
                        otherwise.
  GRADAPP_PORT          Defaults to 5000.
  GRADAPP_PASSWORD      If set, gates every route behind HTTP Basic Auth.
  GRADAPP_MAX_UPLOAD_MB Max request body size in MB. Defaults to 200.
  GRADAPP_FORCE_SECURE_COOKIES  "1" marks the session cookie Secure (only
                        send it over HTTPS). Enable this if you put a TLS
                        proxy in front of GRADAPP_ALLOW_REMOTE -- leave off
                        for plain-HTTP localhost use, or the cookie (and
                        with it CSRF protection) silently stops working.

Also on by default, not configurable: security response headers (CSP,
X-Frame-Options, etc.), per-form CSRF tokens, upload rate limiting, a
file-signature check on top of the extension allow-list, and an access log
at data/audit.log (method/path/status only -- no applicant data).
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Dict, List

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, send_file, session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename

from .run import run, reindex
from .merge import summary_view, sources_present
from .documents import DOC_TYPES, infer_cas_id, ingest_document
from .ingest_zip import ingest_zip
from .store import Store, db_path_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FIELD_MAP = os.path.join(ROOT, "config", "csv_field_map.yaml")

# Extensions accepted per upload route, checked before a file ever touches
# disk. Mirrors what parse.py / documents.extract_text() actually support --
# anything else is refused up front instead of being saved and left to fail
# deep inside the pipeline (or just sit on disk as an unrecognized blob).
CSV_EXTS = {".csv", ".xlsx", ".xls", ".xlsm"}
ZIP_EXTS = {".zip"}
DOC_EXTS = {".pdf", ".docx", ".txt", ".md"}

# File-signature ("magic bytes") checks layered on top of the extension
# allow-list above -- a renamed executable or script still won't pass. Text
# formats (.csv/.txt/.md) have no reliable magic number, so those are instead
# checked for a shebang/PE/ELF header, which a legitimate text file will
# never start with.
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")   # .zip, .docx, .xlsx
_OLE_MAGIC = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)            # legacy .xls
_BINARY_RED_FLAGS = (b"MZ", b"\x7fELF", b"#!")                # PE / ELF / shebang


def _sniff_ok(filename: str, head: bytes) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    if any(head.startswith(flag) for flag in _BINARY_RED_FLAGS):
        return False
    if ext == ".pdf":
        return head.startswith(b"%PDF")
    if ext in (".zip", ".docx", ".xlsx", ".xlsm"):
        return any(head.startswith(m) for m in _ZIP_MAGIC)
    if ext == ".xls":
        return head.startswith(_OLE_MAGIC[0])
    return True   # .csv/.txt/.md: no magic number, red-flag check above is it


def _save_upload(file, dest: str) -> bool:
    """Sniff the file's signature before writing it to disk. Returns False
    (and does not save) if the content doesn't match what the extension
    claims to be."""
    head = file.stream.read(8)
    file.stream.seek(0)
    if not _sniff_ok(file.filename, head):
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    file.save(dest)
    return True


app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))

# Guardrail: never ship a hardcoded secret key -- it's visible to anyone who
# reads this file and would let them forge the signed session/flash cookie.
app.secret_key = os.environ.get("GRADAPP_SECRET_KEY") or secrets.token_hex(32)

# Guardrail: cap request size so one oversized upload can't exhaust memory or
# disk before any of the pipeline's own per-file size checks get a chance to
# run (ingest_zip has its own limits, but they only apply to a ZIP's *inside*).
app.config["MAX_CONTENT_LENGTH"] = (
    int(os.environ.get("GRADAPP_MAX_UPLOAD_MB", "200")) * 1024 * 1024
)

DEBUG = os.environ.get("GRADAPP_DEBUG", "0") == "1"
app.config["DEBUG"] = DEBUG

# Guardrail: session cookie hardening. HTTPONLY is Flask's default already;
# set it explicitly for clarity. SAMESITE=Lax means the cookie (and so the
# CSRF token tied to it) is never sent on a cross-site POST. SECURE is opt-in
# via env var because this app is normally served over plain HTTP on
# localhost -- forcing Secure there would silently break the cookie.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("GRADAPP_FORCE_SECURE_COOKIES") == "1"

# Guardrail: optional access control. Unset by default (fine for a single
# reviewer on localhost); set GRADAPP_PASSWORD to require HTTP Basic Auth on
# every route the moment more than one person can reach this machine.
_PASSWORD = os.environ.get("GRADAPP_PASSWORD")

# Guardrail: rate limiting on the state-changing routes, keyed by client IP.
# Generous enough for one reviewer clicking around, tight enough to blunt a
# scripted upload/auth-guessing loop. In-memory storage is fine for a
# single-process local tool.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://",
                  default_limits=[])

# Guardrail: append-only local access log (method/path/status/remote_addr
# only -- never applicant data) so a reviewer running this for a team can see
# who touched what, when. Configured lazily per-request against the current
# DATA dir rather than bound at import time, so it still respects DATA being
# pointed elsewhere (e.g. in tests).
_audit_logger = logging.getLogger("gradapp.audit")
_audit_logger.setLevel(logging.INFO)


_AUDIT_MAX_BYTES = 5_000_000   # rotate at ~5 MB; one .1 backup is kept


def _audit_log(line: str) -> None:
    try:
        os.makedirs(DATA, exist_ok=True)
        path = os.path.join(DATA, "audit.log")
        try:
            if os.path.getsize(path) > _AUDIT_MAX_BYTES:
                os.replace(path, path + ".1")   # rotate: keep one previous file
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # never let logging failures break a request


@app.before_request
def _require_auth():
    if not _PASSWORD:
        return None
    auth = request.authorization
    # auth.password is None for non-Basic schemes (e.g. Bearer) — treat as failed
    # auth rather than crashing compare_digest with a None.
    if (not auth or not auth.password
            or not secrets.compare_digest(auth.password, _PASSWORD)):
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="grad-app"'})
    return None


@app.before_request
def _same_origin_check():
    """Lightweight first line of CSRF defense: reject state-changing requests
    whose Origin/Referer doesn't match this host, before the (heavier)
    per-form token check below even runs."""
    if request.method != "POST":
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return None  # non-browser clients (CLI, tests) send neither; allow
    netloc = urlparse(origin).netloc
    if netloc and netloc != request.host:
        abort(403)
    return None


def _get_csrf_token() -> str:
    """Session-fixed CSRF token, generated on first use and reused for the
    life of the browser session. Exposed to templates as csrf_token()."""
    tok = session.get("_csrf_token")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf_token"] = tok
    return tok


app.jinja_env.globals["csrf_token"] = _get_csrf_token


@app.before_request
def _csrf_protect():
    """Validate the per-form token against the one tied to this session.
    Skipped in test mode (app.testing) so the test suite can drive routes
    directly without simulating a full GET-then-POST browser flow; the
    same-origin check above still applies even then."""
    if request.method != "POST" or app.testing:
        return None
    sent = request.form.get("csrf_token", "")
    expected = session.get("_csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, description="Missing or invalid CSRF token — reload the page and try again.")
    return None


@app.after_request
def _security_headers(response: Response) -> Response:
    """Defense-in-depth response headers. The CSP allows 'unsafe-inline' for
    script/style because the templates use small inline <script>/style
    blocks rather than a build step -- it still blocks loading any *remote*
    script/style/frame, which is the more likely injection vector here."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
    return response


@app.after_request
def _access_log(response: Response) -> Response:
    _audit_log(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
              f"{request.remote_addr} {request.method} {request.path} -> {response.status_code}")
    return response


def _ext_ok(filename: str, allowed: set) -> bool:
    return os.path.splitext(filename)[1].lower() in allowed


def _store() -> Store:
    return Store(db_path_for(DATA))


def _summaries() -> List[Dict[str, Any]]:
    return _store().index()


def _student_record(cas_id: str) -> Dict[str, Any]:
    return _store().get(cas_id) or {}


@app.route("/")
def index():
    summaries = _summaries()
    quarantine = _store().quarantine()
    flagged = sum(1 for s in summaries if s.get("warnings"))
    return render_template("index.html", summaries=summaries,
                           quarantine=quarantine, flagged=flagged)


@app.route("/upload", methods=["POST"])
@limiter.limit("30/hour")
def upload():
    file = request.files.get("csv")
    if not file or not file.filename:
        flash("Please choose a CSV or Excel file.")
        return redirect(url_for("index"))
    if not _ext_ok(file.filename, CSV_EXTS):
        flash(f"Unsupported file type — use {', '.join(sorted(CSV_EXTS))}.")
        return redirect(url_for("index"))
    fname = secure_filename(file.filename) or "upload.csv"
    dest = os.path.join(DATA, "raw", fname)
    if not _save_upload(file, dest):
        flash("That file's contents don't match its extension — refused.")
        return redirect(url_for("index"))
    try:
        summary = run(dest, DATA, FIELD_MAP)
        flash(f"Processed {summary['rows']} rows — "
              f"{summary['valid']} summarized, {summary['quarantined']} quarantined.")
    except Exception as exc:  # surface parsing errors to the user
        app.logger.exception("Error processing uploaded file %s", fname)
        flash(f"Error processing file: {exc}")
    return redirect(url_for("index"))


def _clear_processed(purge_raw: bool = False) -> int:
    """Clear all processed students + quarantine from the store and delete the
    generated report. By default raw uploads under data/raw/ are left alone.
    Pass purge_raw=True to also delete them -- that's the only way to remove
    an applicant's original file bytes (CSV/PDF/DOCX, still full of PII) from
    disk; merely clearing the browsable records does not do that. Returns
    students cleared."""
    store = _store()
    n = store.clear_students()
    store.clear_quarantine()
    report = os.path.join(DATA, "reports", "_summaries.md")
    try:
        os.remove(report)
    except OSError:
        pass
    # Legacy pre-SQLite stores (data/students/*.json, data/quarantine/*.json)
    # hold the same derived PII as the database — clear them too, or "Clear
    # all" silently leaves full applicant records on disk.
    for legacy in (os.path.join(DATA, "students"), os.path.join(DATA, "quarantine")):
        if os.path.isdir(legacy):
            for fn in os.listdir(legacy):
                if fn.endswith(".json"):
                    try:
                        os.remove(os.path.join(legacy, fn))
                    except OSError:
                        pass
    if purge_raw:
        raw = os.path.join(DATA, "raw")
        for root, _dirs, files in os.walk(raw):
            for fn in files:
                try:
                    os.remove(os.path.join(root, fn))
                except OSError:
                    pass
    return n


@app.route("/reset", methods=["POST"])
@limiter.limit("30/hour")
def reset():
    """Clear all processed students (start fresh). Raw uploads are kept
    unless the 'purge_raw' checkbox was submitted -- check it to actually
    delete applicant files from disk, not just the derived records."""
    purge = bool(request.form.get("purge_raw"))
    n = _clear_processed(purge_raw=purge)
    msg = f"Cleared all processed data ({n} student record(s))."
    if purge:
        msg += " Raw uploaded files were also deleted."
    msg += " Upload to start fresh."
    flash(msg)
    return redirect(url_for("index"))


@app.route("/process", methods=["GET", "POST"])
@limiter.limit("30/hour", methods=["POST"])
def process():
    """Handle the CSV/Excel and/or the ZIP in one submit. CSV is processed first
    (creates the application sections), then the ZIP merges resume/SOP into the
    same records by cas_id."""
    if request.method == "GET":
        return redirect(url_for("index"))
    csv_file = request.files.get("csv")
    zip_file = request.files.get("zip")
    has_csv = bool(csv_file and csv_file.filename)
    has_zip = bool(zip_file and zip_file.filename)
    if not has_csv and not has_zip:
        flash("Choose a CSV/Excel file, a ZIP, or both.")
        return redirect(url_for("index"))

    if has_csv and not _ext_ok(csv_file.filename, CSV_EXTS):
        flash(f"Application data file type not supported — use {', '.join(sorted(CSV_EXTS))}.")
        return redirect(url_for("index"))
    if has_zip and not _ext_ok(zip_file.filename, ZIP_EXTS):
        flash("The packets file must be a .zip.")
        return redirect(url_for("index"))

    msgs = []
    if request.form.get("replace"):
        msgs.append(f"Cleared {_clear_processed()} old record(s).")
    if has_csv:
        dest = os.path.join(DATA, "raw", secure_filename(csv_file.filename) or "upload.csv")
        if not _save_upload(csv_file, dest):
            msgs.append("Application data: file contents don't match its extension — refused.")
        else:
            try:
                s = run(dest, DATA, FIELD_MAP)
                msgs.append(f"Application data: {s['valid']} summarized, {s['quarantined']} quarantined.")
            except Exception as exc:
                app.logger.exception("Error processing application data in /process")
                msgs.append(f"Application data error: {exc}")
    if has_zip:
        dest = os.path.join(DATA, "raw", secure_filename(zip_file.filename) or "packets.zip")
        if not _save_upload(zip_file, dest):
            msgs.append("Packets: file contents don't match its extension — refused.")
        else:
            try:
                r = ingest_zip(dest, DATA)
                m = (f"Packets: {r['pdfs_found']} PDF(s) → updated {r['students_updated']} student(s), "
                     f"added {r['sections_added']} section(s), captured {r['score_sections']} score/transcript section(s).")
                if r.get("scanned_docs"):
                    m += f" OCR read {r['ocr_pages']} scanned page(s) across {r['scanned_docs']} document(s)."
                if r.get("ocr_unavailable_docs"):
                    m += f" ⚠ {r['ocr_unavailable_docs']} document(s) had scanned pages but OCR isn't installed."
                if r["unmatched_files"]:
                    m += f" {len(r['unmatched_files'])} unmatched."
                msgs.append(m)
            except Exception as exc:
                app.logger.exception("Error processing packets ZIP in /process")
                msgs.append(f"Packets error: {exc}")
    flash(" ".join(msgs))
    return redirect(url_for("index"))


@app.route("/upload_zip", methods=["POST"])
@limiter.limit("30/hour")
def upload_zip():
    file = request.files.get("zip")
    if not file or not file.filename:
        flash("Please choose a ZIP file.")
        return redirect(url_for("index"))
    if not _ext_ok(file.filename, ZIP_EXTS):
        flash("The packets file must be a .zip.")
        return redirect(url_for("index"))
    dest = os.path.join(DATA, "raw", secure_filename(file.filename) or "packets.zip")
    if not _save_upload(file, dest):
        flash("That file's contents don't match its extension — refused.")
        return redirect(url_for("index"))
    try:
        r = ingest_zip(dest, DATA)
        msg = (f"Processed {r['pdfs_found']} PDF(s): updated {r['students_updated']} "
               f"student(s), added {r['sections_added']} section(s), "
               f"captured {r['score_sections']} score/transcript section(s).")
        if r["unmatched_files"]:
            msg += f" {len(r['unmatched_files'])} file(s) could not be matched."
        flash(msg)
    except Exception as exc:
        app.logger.exception("Error processing ZIP in /upload_zip")
        flash(f"Error processing ZIP: {exc}")
    return redirect(url_for("index"))


@app.route("/upload_document", methods=["POST"])
@limiter.limit("60/hour")
def upload_document():
    file = request.files.get("doc")
    doc_type = (request.form.get("type") or "").strip()
    cas_id = (request.form.get("cas_id") or "").strip()
    recommender = (request.form.get("recommender") or "").strip() or None
    if not file or not file.filename:
        flash("Please choose a document.")
        return redirect(url_for("index"))
    if doc_type not in DOC_TYPES:
        flash(f"Type must be one of {', '.join(DOC_TYPES)}.")
        return redirect(url_for("index"))
    if not _ext_ok(file.filename, DOC_EXTS):
        flash(f"Unsupported file type — use {', '.join(sorted(DOC_EXTS))}.")
        return redirect(url_for("index"))
    fname = secure_filename(file.filename)
    cas_id = cas_id or infer_cas_id(fname)
    if not cas_id:
        flash("Could not determine cas_id — enter it, or name the file '<cas_id>_sop.pdf'.")
        return redirect(url_for("index"))
    # Guard against typos creating phantom student records: this route only
    # attaches to an existing student. (The CLI, src.add_document, can still
    # create a record when documents arrive before the application data.)
    if not _store().get(cas_id):
        flash(f"No student '{cas_id}' on file — document not attached. Check the ID, "
              "or upload the application data first.")
        return redirect(url_for("index"))
    dest = os.path.join(DATA, "raw", "documents", fname)
    if not _save_upload(file, dest):
        flash("That file's contents don't match its extension — refused.")
        return redirect(url_for("index"))
    try:
        _, words = ingest_document(dest, cas_id, doc_type, DATA, recommender=recommender)
        reindex(DATA)
        flash(f"Added {doc_type} ({words} words) to {cas_id}.")
    except Exception as exc:
        app.logger.exception("Error reading document for %s in /upload_document", cas_id)
        flash(f"Error reading document: {exc}")
    return redirect(url_for("student", cas_id=cas_id))


@app.route("/summaries")
def summaries():
    return render_template("summaries.html", summaries=_summaries())


@app.route("/download/summaries")
def download_summaries():
    path = os.path.join(DATA, "reports", "_summaries.md")
    if not os.path.exists(path):
        flash("No summaries yet — upload a file first.")
        return redirect(url_for("index"))
    return send_file(path, as_attachment=True, download_name="applicant_summaries.md")


@app.route("/student/<cas_id>")
def student(cas_id: str):
    unified = _student_record(cas_id)
    if not unified:
        abort(404)
    rec = summary_view(unified)                 # flattened view for the template
    summary = {"summary_text": unified.get("summary", "")}
    return render_template("student.html", rec=rec, summary=summary,
                           unified=unified, sources_present=sources_present(unified))


if __name__ == "__main__":
    host = os.environ.get("GRADAPP_HOST", "127.0.0.1")
    port = int(os.environ.get("GRADAPP_PORT", "5000"))
    allow_remote = os.environ.get("GRADAPP_ALLOW_REMOTE") == "1"

    # Guardrail: refuse to bind beyond localhost unless explicitly opted in,
    # and never without auth -- this app renders applicant PII with no login
    # by default.
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        raise SystemExit(
            f"Refusing to bind to a non-loopback address ({host!r}) without "
            "GRADAPP_ALLOW_REMOTE=1. This app renders applicant PII (names, "
            "emails, test scores, SOP/LOR text) with no auth by default.")
    if allow_remote and not _PASSWORD:
        raise SystemExit(
            "GRADAPP_ALLOW_REMOTE=1 requires GRADAPP_PASSWORD to also be "
            "set -- refusing to serve applicant PII on a non-loopback "
            "address with no authentication.")
    if DEBUG:
        print("⚠ GRADAPP_DEBUG=1: the interactive Werkzeug debugger is "
              "enabled. Never set this if the process is reachable by "
              "anyone you would not hand a shell to.", flush=True)

    # use_reloader=False is important: uploads are saved under data/, and the
    # auto-reloader would otherwise restart the server mid-upload and kill the
    # request. Keep the friendly debugger pages only when DEBUG is explicitly
    # requested, but never restart-on-file-write.
    app.run(host=host, port=port, debug=DEBUG, use_reloader=False)
